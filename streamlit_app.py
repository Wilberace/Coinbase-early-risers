import time, smtplib, ssl
from email.mime.text import MIMEText
from statistics import median
from datetime import datetime, timezone
import streamlit as st

# ===================== SETTINGS (tighter, less noise) =====================
QUOTES = {"USD", "USDT"}
MAX_PRICE = 1000.0
MIN_PRICE = 0.03            # ignore sub-$0.03 dust
MIN_24H_USD_VOL = 1_500_000 # (kept for future; not used with ccxt public data)

# Finals (harder to trigger = more selective)
MIN_5M_PCT    = 1.2
MIN_15M_PCT   = 2.2
NEAR_HIGH_PCT = 3.0
VOL_SURGE_X   = 2.2
MAX_SPREAD_BPS = 25.0

# Early (still earlier than finals, but less chatty)
EARLY_NEAR_LOCAL_PCT_MAX = 5.0
EARLY_5M_MIN = 0.2
EARLY_5M_MAX = 0.9
EARLY_15M_MAX = 1.8
EARLY_VOL_X_MIN = 1.5
EARLY_TOP = 15

# Bottoming (conservative; set BOTTOM_TOP=0 to hide the list)
BOTTOM_LOOKBACK_MIN = 90
BOTTOM_NEAR_LOW_PCT_MAX = 1.2
BOTTOM_UP1M_MIN = 0.08
BOTTOM_5M_MIN = 0.15
BOTTOM_5M_MAX = 0.60
BOTTOM_VOL_X_MIN = 1.20
BOTTOM_TOP = 10

# Refresh & alert cooldown
REFRESH_SEC = 45
COOLDOWN_MIN = 10      # minimum minutes between emails/SMS

# ===================== EMAIL / SMS (Streamlit Secrets) =====================
# Add in Streamlit Cloud → Manage app → Settings → Secrets:
# [smtp]
# host = "smtp.gmail.com"
# port = 465
# user = "your_gmail@gmail.com"
# pass = "your_16_char_app_password"     # or Mailjet/SendGrid creds
# to = ["you@example.com", "1234567890@vtext.com"]  # SMS via carrier gateway

def build_report_text(bottom_rows, early_rows, final_rows):
    lines = []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Coinbase Early Risers Report — {now}")
    lines.append("")

    def add_table(title, rows, limit=None):
        lines.append(title)
        if rows:
            lines.append("Symbol       Price        1m%   5m%   15m%  Near%  Vol5m_x")
            use_rows = rows if (limit is None) else rows[:limit]
            for r in use_rows:
                sym   = f"{r.get('Symbol',''):<11s}"
                price = f"{r.get('Price',0):<11.6f}"
                p1  = "" if r.get("1m%") is None else f"{r.get('1m%',0):.2f}"
                p5  = "" if r.get("5m%") is None else f"{r.get('5m%',0):.2f}"
                p15 = "" if r.get("15m%") is None else f"{r.get('15m%',0):.2f}"
                near = "" if r.get("NearHigh%") is None else f"{r.get('NearHigh%',0):.2f}"
                vx   = "" if r.get("Vol5m_x") is None else f"{r.get('Vol5m_x',0):.2f}"
                lines.append(f"{sym} {price} {p1:>5} {p5:>5} {p15:>6} {near:>5} {vx:>7}")
        else:
            lines.append("(none)")
        lines.append("")

    if BOTTOM_TOP > 0:
        add_table("BOTTOMING SETUPS (near local low, curling up)", bottom_rows, BOTTOM_TOP)
    add_table("EARLY WATCHLIST (pre-breakout)", early_rows, EARLY_TOP)
    add_table("CLEAN RISERS (final checks)", final_rows, None)

    return "\n".join(lines)

def send_report(bottom_rows, early_rows, final_rows, subject_suffix="Report"):
    try:
        smtp = st.secrets["smtp"]
        host = smtp.get("host", "smtp.gmail.com")
        port = int(smtp.get("port", 465))
        user = smtp["user"]
        pwd  = smtp["pass"]
        to   = list(smtp["to"])
    except Exception:
        # No secrets configured → skip silently
        return
    body = build_report_text(bottom_rows, early_rows, final_rows)
    msg = MIMEText(body)
    msg["Subject"] = f"Coinbase Early Risers — {subject_suffix}"
    msg["From"] = user
    msg["To"] = ", ".join(to)
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as server:
            server.login(user, pwd)
            server.sendmail(user, to, msg.as_string())
        st.toast("📨 Report sent", icon="✉️")
    except Exception as e:
        st.warning(f"Alert/report error: {e}")

# ===================== DEPENDENCIES =====================
try:
    import ccxt
except ImportError:
    st.error("Missing ccxt. Streamlit Cloud will install from requirements.txt.")
    st.stop()

# ===================== HELPERS =====================
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

def last_n_green_ratio(ohlcv, n=3):
    if len(ohlcv) < n:
        return 0.0
    greens = sum(1 for c in ohlcv[-n:] if c[4] >= c[1])  # close >= open
    return greens / n

def is_strict_up(closes, n=3):
    if len(closes) < n:
        return False
    seq = closes[-n:]
    return all(seq[i] > seq[i-1] for i in range(1, len(seq)))

# ===================== SCANNER =====================
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
    bottoms, early, finals = [], [], []

    for sym in symbols:
        ohlcv = fetch_ohlcv_safe(ex, sym, limit=max(200, BOTTOM_LOOKBACK_MIN + 5))
        time.sleep(rl)
        if too_old(ohlcv) or len(ohlcv) < 60:
            continue

        opens  = [c[1] for c in ohlcv]
        highs  = [c[2] for c in ohlcv]
        closes = [c[4] for c in ohlcv]
        vols   = [c[5] for c in ohlcv]

        try:
            t = ex.fetch_ticker(sym)
            last_close = float(t.get("last") or t.get("close"))
        except Exception:
            last_close = float(closes[-1])

        if last_close < MIN_PRICE or last_close > MAX_PRICE:
            continue

        vol_med = max(1e-9, float(median(vols)))

        def last_n(n):
            if len(closes) < n+1:
                return float("nan")
            return pct(closes[-1], closes[-(n+1)])

        p1  = pct(closes[-1], closes[-2]) if len(closes) >= 2 else float("nan")
        p5  = last_n(5)
        p15 = last_n(15)
        p60 = last_n(60)
        vol_5m = float(sum(vols[-5:]))
        volx = vol_5m / (vol_med * 5.0)

        px_local_high = max(highs) if highs else 0.0
        dist_local = (px_local_high - last_close) / (px_local_high or 1) * 100.0 if px_local_high > 0 else 100.0
        near_local = 100.0 - dist_local

        green3 = last_n_green_ratio(ohlcv, 3)  # ≥ 2/3 green candles desired
        strict3 = is_strict_up(closes, 3)      # closes rising last 3 bars

        # -------- Bottoming --------
        if BOTTOM_TOP > 0:
            lookback = min(BOTTOM_LOOKBACK_MIN, len(closes)-1)
            if lookback >= 30:
                window = closes[-lookback:]
                loc_low = min(window) if window else 0.0
                dist_low = (last_close - loc_low) / (loc_low or 1) * 100.0 if loc_low > 0 else 0.0
                if (dist_low <= BOTTOM_NEAR_LOW_PCT_MAX and
                    p1 == p1 and p1 >= BOTTOM_UP1M_MIN and
                    p5 == p5 and BOTTOM_5M_MIN <= p5 <= BOTTOM_5M_MAX and
                    volx >= BOTTOM_VOL_X_MIN and
                    green3 >= (2/3)):
                    bottoms.append({
                        "Symbol": sym, "Price": round(last_close,6),
                        "1m%": round(p1,2) if p1==p1 else None,
                        "5m%": round(p5,2) if p5==p5 else None,
                        "15m%": round(p15,2) if p15==p15 else None,
                        "NearHigh%": round(near_local,2),
                        "Vol5m_x": round(volx,2),
                    })

        # -------- Early Watchlist --------
        if (dist_local <= EARLY_NEAR_LOCAL_PCT_MAX and
            p5==p5 and EARLY_5M_MIN <= p5 <= EARLY_5M_MAX and
            p15==p15 and p15 <= EARLY_15M_MAX and
            volx >= EARLY_VOL_X_MIN and
            green3 >= (2/3) and
            strict3):
            early.append({
                "Symbol": sym, "Price": round(last_close,6),
                "1m%": round(p1,2) if p1==p1 else None,
                "5m%": round(p5,2) if p5==p5 else None,
                "15m%": round(p15,2) if p15==p15 else None,
                "NearHigh%": round(near_local,2),
                "Vol5m_x": round(volx,2),
            })

        # -------- Clean Risers (final) --------
        if (p5==p5 and p5>=MIN_5M_PCT and
            p15==p15 and p15>=MIN_15M_PCT and
            volx >= VOL_SURGE_X and
            dist_local <= NEAR_HIGH_PCT and
            green3 >= (2/3) and
            strict3):
            finals.append({
                "Symbol": sym, "Price": round(last_close,6),
                "1m%": round(p1,2) if p1==p1 else None,
                "5m%": round(p5,2) if p5==p5 else None,
                "15m%": round(p15,2) if p15==p15 else None,
                "NearHigh%": round(100.0 - dist_local,2),
                "Vol5m_x": round(volx,2),
            })

    # Sorts
    if BOTTOM_TOP > 0:
        bottoms = sorted(bottoms, key=lambda r: (r.get("5m%") or 0, r.get("Vol5m_x") or 0, -(r.get("NearHigh%") or 0)), reverse=True)[:BOTTOM_TOP]
    early   = sorted(early,   key=lambda r: (r.get("15m%") or 0, r.get("5m%") or 0, r.get("Vol5m_x") or 0), reverse=True)[:EARLY_TOP]
    finals  = sorted(finals,  key=lambda r: (r.get("15m%") or 0, r.get("5m%") or 0, r.get("Vol5m_x") or 0), reverse=True)
    return bottoms, early, finals

# ===================== UI =====================
st.set_page_config(page_title="Coinbase Early Risers (Tight/Low-Noise)", layout="wide")
st.title("📈 Coinbase Early Risers — Low-Noise Mode")

bottoms, early, finals = scan()
st.write(f"Last update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# Init session state
if "seen_early" not in st.session_state:
    st.session_state["seen_early"] = set()
if "last_report_ts" not in st.session_state:
    st.session_state["last_report_ts"] = 0.0

# Bottoming table
if BOTTOM_TOP > 0:
    if bottoms:
        st.subheader("Bottoming Setups (near local low, curling up)")
        st.dataframe(bottoms, use_container_width=True)
    else:
        st.info("No bottoming setups right now.")

# Early table
if early:
    st.subheader("Early Watchlist (pre-breakout)")
    st.dataframe(early, use_container_width=True)
    curr_e_syms = [row["Symbol"] for row in early]
    unseen_e = [s for s in curr_e_syms if s not in st.session_state["seen_early"]]
else:
    unseen_e = []
    st.info("No early candidates right now.")

# Finals table
if finals:
    st.subheader("Clean Risers (final checks)")
    st.dataframe(finals, use_container_width=True)
else:
    st.warning("No clean risers right now.")

# Send full report when NEW Early names appear (with cooldown)
if unseen_e:
    now_ts = time.time()
    if now_ts - st.session_state["last_report_ts"] >= COOLDOWN_MIN * 60:
        send_report(bottoms, early, finals, subject_suffix="New Early Candidates")
        st.session_state["last_report_ts"] = now_ts
    st.session_state["seen_early"].update(unseen_e)

# Manual test report
if st.button("Send test report now"):
    send_report(bottoms, early, finals, subject_suffix="Manual Test")
    st.success("Test report sent (check email/text).")

# Auto-refresh
time.sleep(REFRESH_SEC)
st.rerun()
