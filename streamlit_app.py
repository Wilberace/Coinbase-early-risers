import time, smtplib, ssl
from email.mime.text import MIMEText
from statistics import median
from datetime import datetime, timezone
import streamlit as st

# ===== SETTINGS =====
QUOTES = {"USD", "USDT"}
MAX_PRICE = 1000.0
MIN_PRICE = 0.01
MIN_24H_USD_VOL = 1_500_000

# Finals thresholds
MIN_5M_PCT    = 1.2
MIN_15M_PCT   = 2.5
NEAR_HIGH_PCT = 3.0
VOL_SURGE_X   = 2.5
MAX_SPREAD_BPS = 25.0

# Early thresholds
EARLY_NEAR_LOCAL_PCT_MAX = 5.0
EARLY_5M_MIN = 0.2
EARLY_5M_MAX = 1.2
EARLY_15M_MAX = 2.5
EARLY_VOL_X_MIN = 1.4
EARLY_TOP = 15

# Auto-refresh seconds
REFRESH_SEC = 60

# ===== ALERTS (set in Streamlit Cloud → Settings → Secrets) =====
# [smtp]
# host = "smtp.gmail.com"
# port = 465
# user = "your_gmail@gmail.com"
# pass = "your_16_char_app_password"
# to = ["youremail@example.com", "1234567890@vtext.com"]
def send_alert(new_symbols):
    try:
        smtp = st.secrets["smtp"]
        host = smtp.get("host", "smtp.gmail.com")
        port = int(smtp.get("port", 465))
        user = smtp["user"]
        pwd  = smtp["pass"]
        to   = list(smtp["to"])
    except Exception:
        # No secrets configured → silently skip alerts
        return
    if not new_symbols:
        return
    body = "New Early Candidates ({}):\n\n{}".format(
        datetime.now().strftime("%Y-%m-%d %H:%M"), "\n".join(new_symbols)
    )
    msg = MIMEText(body)
    msg["Subject"] = "Coinbase Early Risers Alert {}".format(datetime.now().strftime("%H:%M"))
    msg["From"] = user
    msg["To"] = ", ".join(to)
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as server:
            server.login(user, pwd)
            server.sendmail(user, to, msg.as_string())
        st.toast("📨 Alert sent: {}".format(", ".join(new_symbols)), icon="✉️")
    except Exception as e:
        st.warning(f"Alert error: {e}")

# ===== DEPENDENCIES =====
try:
    import ccxt
except ImportError:
    st.error("Missing ccxt. Streamlit Cloud will install from requirements.txt.")
    st.stop()

# ===== HELPERS =====
def pct(curr, prev):
    try:
        return (curr - prev) / prev * 100.0
    except Exception:
        return float("nan")

def fetch_ohlcv_safe(ex, symbol, limit=200):
    try:
        return ex.fetch_ohlcv(symbol, timeframe="1m", limit=limit) or []
    except Exception:
        return []

def too_old(candles, max_age_min=6):
    if not candles:
        return True
    last_ts = candles[-1][0]
    age_min = (datetime.now(timezone.utc).timestamp()*1000 - last_ts)/60000
    return age_min > max_age_min

# ===== SCANNER =====
def scan():
    ex = ccxt.coinbase()
    ex.enableRateLimit = True
    ex.load_markets()

    skip_tickers = {"USDT", "USD", "USD1", "USDC"}
    skip_symbols = {"USDT/USD", "USD1/USD"}
    symbols = []
    for m in ex.markets.values():
        if not m.get("active", True): continue
        if not m.get("spot", False): continue
        if m.get("quote") in QUOTES:
            sym = m["symbol"]
            base = sym.split("/")[0]
            if base in skip_tickers or sym in skip_symbols: continue
            symbols.append(sym)

    rl = max(getattr(ex, "rateLimit", 120), 120) / 1000.0
    early, finals = [], []

    for sym in symbols:
        ohlcv = fetch_ohlcv_safe(ex, sym, limit=200)
        time.sleep(rl)
        if too_old(ohlcv) or len(ohlcv) < 60:
            continue

        closes = [c[4] for c in ohlcv]
        vols   = [c[5] for c in ohlcv]
        highs  = [c[2] for c in ohlcv]

        try:
            t = ex.fetch_ticker(sym)
            last_close = float(t.get("last") or t.get("close"))
        except Exception:
            last_close = float(closes[-1])

        if last_close < MIN_PRICE or last_close > MAX_PRICE:
            continue

        vol_med = max(1e-9, float(median(vols)))
        def last_n(n):
            if len(closes) < n+1: return float("nan")
            return pct(closes[-1], closes[-(n+1)])
        p5, p15, p60 = last_n(5), last_n(15), last_n(60)
        vol_5m = float(sum(vols[-5:]))
        volx = vol_5m / (vol_med * 5.0)

        px_local_high = max(highs) if highs else 0.0
        dist_local = (px_local_high - last_close) / (px_local_high or 1) * 100.0 if px_local_high>0 else 100.0
        near_local = 100.0 - dist_local

        # ---- Early Watchlist ----
        if (dist_local <= EARLY_NEAR_LOCAL_PCT_MAX and
            p5==p5 and EARLY_5M_MIN <= p5 <= EARLY_5M_MAX and
            p15==p15 and p15 <= EARLY_15M_MAX and
            volx >= EARLY_VOL_X_MIN):
            early.append({
                "Symbol": sym, "Price": round(last_close,6),
                "5m%": round(p5,2) if p5==p5 else None,
                "15m%": round(p15,2) if p15==p15 else None,
                "NearHigh%": round(near_local,2),
                "Vol5m_x": round(volx,2),
            })

        # ---- Final Candidates ----
        if (p5==p5 and p5>=MIN_5M_PCT and
            p15==p15 and p15>=MIN_15M_PCT and
            volx >= VOL_SURGE_X and
            dist_local <= NEAR_HIGH_PCT):
            finals.append({
                "Symbol": sym, "Price": round(last_close,6),
                "5m%": round(p5,2) if p5==p5 else None,
                "15m%": round(p15,2) if p15==p15 else None,
                "NearHigh%": round(100.0 - dist_local,2),
                "Vol5m_x": round(volx,2),
            })

    early = sorted(early, key=lambda r: (r.get("15m%") or 0, r.get("5m%") or 0, r.get("Vol5m_x") or 0), reverse=True)[:EARLY_TOP]
    finals = sorted(finals, key=lambda r: (r.get("15m%") or 0, r.get("5m%") or 0, r.get("Vol5m_x") or 0), reverse=True)
    return early, finals

# ===== UI =====
st.set_page_config(page_title="Coinbase Early Risers (Alerts)", layout="wide")
st.title("📈 Coinbase Early Risers — Alerts Enabled")

early, finals = scan()
st.write(f"Last update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# Initialize seen set in session
if "seen_early" not in st.session_state:
    st.session_state["seen_early"] = set()

if early:
    st.subheader("Early Watchlist (pre-breakout)")
    st.dataframe(early, use_container_width=True)
    current_syms = [row["Symbol"] for row in early]
    unseen = [s for s in current_syms if s not in st.session_state["seen_early"]]
    if unseen:
        send_alert(unseen)
        st.session_state["seen_early"].update(unseen)
else:
    st.info("No early candidates right now.")

if finals:
    st.subheader("Clean Risers (final checks)")
    st.dataframe(finals, use_container_width=True)
else:
    st.warning("No clean risers right now.")

# Auto-refresh
time.sleep(REFRESH_SEC)
st.rerun()
