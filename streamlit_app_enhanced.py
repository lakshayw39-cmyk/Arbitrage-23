"""
Enhanced Cross-Exchange Crypto Arbitrage Paper Scanner
======================================================

Deploy on Streamlit Cloud:
  repo root must contain:
    streamlit_app.py      <- this file
    requirements.txt      <- ccxt, streamlit, pandas

Features:
  - 25+ exchanges (including tier-2 venues with wider spreads)
  - 60+ base currencies with USDT/USDC/USD/TUSD/DAI quotes
  - Diagnostic mode (shows ALL spreads, even negative, to debug)
  - Cross-quote (stablecoin) arbitrage detection
  - Connection tester
  - Per-exchange fee editor
  - Raw order book viewer
  - Best spread tracking across session
  - Symbol presets (Memes, Layer 1s, DeFi, Major)
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import defaultdict

# Self-healing import
try:
    import ccxt
except ModuleNotFoundError:
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "ccxt>=4.5"])
    import ccxt

import pandas as pd
import streamlit as st

# ============================================================================
# CONSTANTS
# ============================================================================

ALL_EXCHANGES = [
    "kraken", "kucoin", "okx", "binance", "bybit", "gate", "coinbase",
    "mexc", "bitget", "bitstamp", "bitfinex", "poloniex", "whitebit",
    "lbank", "digifinex", "xt", "phemex", "coinex", "ascendex", "probit",
    "hitbtc", "bitmart", "exmo", "bkex", "bigone"
]

BASE_CURRENCIES = [
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK",
    "MATIC", "DOT", "LTC", "BCH", "UNI", "AAVE", "COMP", "MKR",
    "CRV", "SUSHI", "1INCH", "YFI", "SNX", "BAT", "MANA", "SAND",
    "AXS", "FIL", "TRX", "ETC", "XLM", "XMR", "ALGO", "VET",
    "ICP", "NEAR", "APT", "ARB", "OP", "LDO", "RNDR", "GRT",
    "FTM", "EOS", "IOTA", "NEO", "THETA", "KAVA", "CHZ", "ENJ",
    "ZRX", "KNC", "BAL", "OCEAN", "BAND", "DYDX", "IMX", "PEPE",
    "SHIB", "FLOKI", "WIF", "BONK", "TIA", "SEI", "SUI", "JUP",
    "PYTH", "WLD", "STRK", "BLUR", "ORDI", "SATS", "BEAM", "KAS",
    "RUNE", "INJ", "GALA", "ILV", "MASK", "CELO", "ROSE"
]

QUOTE_CURRENCIES = ["USDT", "USDC", "USD", "TUSD", "DAI"]

DEFAULT_TAKER_FEES = {
    "binance": 0.0010, "kraken": 0.0026, "kucoin": 0.0010,
    "bybit": 0.0010, "okx": 0.0010, "gate": 0.0020,
    "coinbase": 0.0060, "mexc": 0.0020, "bitget": 0.0010,
    "bitstamp": 0.0050, "bitfinex": 0.0020, "poloniex": 0.0020,
    "whitebit": 0.0010, "lbank": 0.0020, "digifinex": 0.0020,
    "xt": 0.0020, "phemex": 0.0010, "coinex": 0.0020,
    "ascendex": 0.0020, "probit": 0.0020, "hitbtc": 0.0020,
    "bitmart": 0.0020, "exmo": 0.0020, "bkex": 0.0020, "bigone": 0.0020
}

ORDER_BOOK_DEPTH = 20
MAX_WORKERS = 24

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class Quote:
    exchange: str
    symbol: str
    vwap_ask: float
    vwap_bid: float
    best_ask: float
    best_bid: float
    ask_depth_usd: float
    bid_depth_usd: float
    latency_ms: float
    timestamp: str

@dataclass
class HealthStatus:
    ok: int = 0
    fail: int = 0
    last_err: str = ""
    avg_latency_ms: float = 0.0
    last_success: str = ""

# ============================================================================
# CORE FUNCTIONS
# ============================================================================

def vwap_through_book(levels, target_usd):
    """Depth-weighted average price to fill target_usd notional. None if thin."""
    if not levels or target_usd <= 0:
        return None
    remaining, cost, qty = target_usd, 0.0, 0.0
    for price, size in levels:
        if price <= 0 or size <= 0:
            continue
        take_usd = min(remaining, price * size)
        take_qty = take_usd / price
        cost += take_qty * price
        qty += take_qty
        remaining -= take_usd
        if remaining <= 1e-9:
            return cost / qty
    return None

def calculate_depth(levels, max_usd=None):
    """Calculate total USD depth in order book."""
    total = 0.0
    for price, size in levels:
        if price > 0 and size > 0:
            total += price * size
            if max_usd and total >= max_usd:
                return max_usd
    return total

@st.cache_resource(show_spinner=False)
def get_clients(exchange_ids):
    """Initialize exchange clients with market loading."""
    clients = {}
    for ex_id in exchange_ids:
        try:
            cls = getattr(ccxt, ex_id)
            client = cls({
                "enableRateLimit": True,
                "timeout": 15000,
                "options": {"defaultType": "spot"}
            })
            client.load_markets()
            clients[ex_id] = client
        except Exception:
            pass
    return clients

def fetch_quote(client, ex_id, symbol, clip_usd):
    """Fetch order book and calculate VWAP quote."""
    t0 = time.time()
    try:
        ob = client.fetch_order_book(symbol, limit=ORDER_BOOK_DEPTH)
        latency_ms = (time.time() - t0) * 1000

        if not ob or "asks" not in ob or "bids" not in ob:
            return None, ex_id, "empty response", 0.0
        if not ob["asks"] or not ob["bids"]:
            return None, ex_id, "empty book", latency_ms

        vwap_ask = vwap_through_book(ob["asks"], clip_usd)
        vwap_bid = vwap_through_book(ob["bids"], clip_usd)
        if vwap_ask is None or vwap_bid is None:
            return None, ex_id, "thin book", latency_ms

        ask_depth = calculate_depth(ob["asks"], clip_usd * 2)
        bid_depth = calculate_depth(ob["bids"], clip_usd * 2)

        q = Quote(
            exchange=ex_id, symbol=symbol,
            vwap_ask=vwap_ask, vwap_bid=vwap_bid,
            best_ask=ob["asks"][0][0] if ob["asks"] else None,
            best_bid=ob["bids"][0][0] if ob["bids"] else None,
            ask_depth_usd=ask_depth, bid_depth_usd=bid_depth,
            latency_ms=latency_ms,
            timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S")
        )
        return q, ex_id, "ok", latency_ms

    except ccxt.ExchangeNotAvailable:
        return None, ex_id, "geo-blocked", (time.time()-t0)*1000
    except ccxt.RequestTimeout:
        return None, ex_id, "timeout", (time.time()-t0)*1000
    except ccxt.BadSymbol:
        return None, ex_id, "not listed", (time.time()-t0)*1000
    except ccxt.BaseError as e:
        return None, ex_id, type(e).__name__, (time.time()-t0)*1000
    except Exception as e:
        return None, ex_id, type(e).__name__[:30], (time.time()-t0)*1000

def scan(clients, symbols, clip_usd):
    """Scan all exchanges and symbols for quotes."""
    quotes = []
    health = {ex: HealthStatus() for ex in clients}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(fetch_quote, cl, ex_id, sym, clip_usd)
            for ex_id, cl in clients.items()
            for sym in symbols
        ]
        for fut in as_completed(futures):
            q, ex_id, status, latency = fut.result()
            if q:
                quotes.append(q)
                health[ex_id].ok += 1
                if health[ex_id].avg_latency_ms == 0:
                    health[ex_id].avg_latency_ms = latency
                else:
                    health[ex_id].avg_latency_ms = (
                        health[ex_id].avg_latency_ms * 0.9 + latency * 0.1
                    )
                health[ex_id].last_success = q.timestamp
            else:
                health[ex_id].fail += 1
                health[ex_id].last_err = status

    return quotes, health

def find_opportunities(quotes, clip_usd, taker_fees, min_net_bps=None, diagnostic=False):
    """Find arbitrage opportunities across all quote combinations."""
    rows = []
    by_symbol = defaultdict(list)
    for q in quotes:
        by_symbol[q.symbol].append(q)

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    for symbol, qs in by_symbol.items():
        for b in qs:
            for s in qs:
                if b.exchange == s.exchange:
                    continue
                gross_bps = (s.vwap_bid - b.vwap_ask) / b.vwap_ask * 10000
                fees_bps = (taker_fees.get(b.exchange, 0.002) + 
                           taker_fees.get(s.exchange, 0.002)) * 10000
                net_bps = gross_bps - fees_bps

                row = {
                    "time": now, "symbol": symbol,
                    "buy on": b.exchange, "sell on": s.exchange,
                    "buy vwap": round(b.vwap_ask, 6),
                    "sell vwap": round(s.vwap_bid, 6),
                    "best ask": round(b.best_ask, 6) if b.best_ask else None,
                    "best bid": round(s.best_bid, 6) if s.best_bid else None,
                    "gross bps": round(gross_bps, 2),
                    "fees bps": round(fees_bps, 2),
                    "net bps": round(net_bps, 2),
                    "net pnl $": round(clip_usd * net_bps / 10000, 2),
                    "buy depth $": round(b.ask_depth_usd, 0),
                    "sell depth $": round(s.bid_depth_usd, 0),
                    "buy latency ms": round(b.latency_ms, 0),
                    "sell latency ms": round(s.latency_ms, 0),
                }

                if diagnostic:
                    rows.append(row)
                elif net_bps > 0 and (min_net_bps is None or net_bps >= min_net_bps):
                    rows.append(row)

    return sorted(rows, key=lambda r: r["net bps"], reverse=True)

def find_cross_quote_opportunities(quotes, taker_fees, min_net_bps=1.0):
    """Find stablecoin arbitrage: e.g., BTC/USDT vs BTC/USDC."""
    rows = []
    by_base = defaultdict(lambda: defaultdict(list))

    for q in quotes:
        try:
            base, quote = q.symbol.split("/")
            by_base[base][quote].append(q)
        except ValueError:
            continue

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    for base, quote_groups in by_base.items():
        quotes_list = list(quote_groups.items())
        for i in range(len(quotes_list)):
            for j in range(i + 1, len(quotes_list)):
                q1_name, q1_quotes = quotes_list[i]
                q2_name, q2_quotes = quotes_list[j]

                if not q1_quotes or not q2_quotes:
                    continue

                best_ask_q1 = min(q1_quotes, key=lambda q: q.vwap_ask)
                best_bid_q1 = max(q1_quotes, key=lambda q: q.vwap_bid)
                best_ask_q2 = min(q2_quotes, key=lambda q: q.vwap_ask)
                best_bid_q2 = max(q2_quotes, key=lambda q: q.vwap_bid)

                implied_1 = best_ask_q1.vwap_ask / best_bid_q2.vwap_bid
                dev_1_bps = (implied_1 - 1.0) * 10000

                implied_2 = best_ask_q2.vwap_ask / best_bid_q1.vwap_bid
                dev_2_bps = (implied_2 - 1.0) * 10000

                if dev_1_bps > dev_2_bps:
                    direction = f"Buy {base}/{q1_name} → Sell {base}/{q2_name}"
                    dev_bps = dev_1_bps
                    buy_ex = best_ask_q1.exchange
                    sell_ex = best_bid_q2.exchange
                    buy_px = best_ask_q1.vwap_ask
                    sell_px = best_bid_q2.vwap_bid
                    implied = implied_1
                else:
                    direction = f"Buy {base}/{q2_name} → Sell {base}/{q1_name}"
                    dev_bps = dev_2_bps
                    buy_ex = best_ask_q2.exchange
                    sell_ex = best_bid_q1.exchange
                    buy_px = best_ask_q2.vwap_ask
                    sell_px = best_bid_q1.vwap_bid
                    implied = implied_2

                if dev_bps > 0:
                    fees_bps = (taker_fees.get(buy_ex, 0.002) + 
                               taker_fees.get(sell_ex, 0.002)) * 10000
                    net_bps = dev_bps - fees_bps

                    if net_bps >= min_net_bps:
                        rows.append({
                            "time": now, "base": base, "direction": direction,
                            "buy on": buy_ex, "sell on": sell_ex,
                            "buy px": round(buy_px, 6), "sell px": round(sell_px, 6),
                            "implied rate": round(implied, 6),
                            "gross bps": round(dev_bps, 2),
                            "fees bps": round(fees_bps, 2),
                            "net bps": round(net_bps, 2),
                        })

    return sorted(rows, key=lambda r: r["net bps"], reverse=True)

# ============================================================================
# UI
# ============================================================================

st.set_page_config(page_title="Enhanced Crypto Arb Scanner", layout="wide")
st.title("🔍 Enhanced Cross-Exchange Arbitrage Scanner")
st.caption(
    "Expanded exchanges, expanded assets, diagnostic tools, cross-quote arbitrage, "
    "and paper trading. **Paper trading only.**"
)

# --- Sidebar ---
with st.sidebar:
    st.header("⚙️ Configuration")

    st.subheader("Exchanges")
    exchanges = st.multiselect(
        "Select exchanges",
        ALL_EXCHANGES,
        default=["kraken", "kucoin", "okx", "gate", "mexc", "bitget", 
                "whitebit", "lbank", "digifinex", "xt", "coinex"]
    )

    st.subheader("Trading Pairs")
    quote_currency = st.selectbox("Quote currency", QUOTE_CURRENCIES, index=0)
    available_symbols = [f"{base}/{quote_currency}" for base in BASE_CURRENCIES]

    preset = st.selectbox(
        "Symbol preset",
        ["Custom", "Major 15", "Major 30", "Meme coins", "Layer 1s", "DeFi tokens"]
    )

    if preset == "Major 15":
        default_symbols = available_symbols[:15]
    elif preset == "Major 30":
        default_symbols = available_symbols[:30]
    elif preset == "Meme coins":
        meme_bases = ["PEPE", "SHIB", "FLOKI", "WIF", "BONK", "DOGE", "SATS", "ORDI"]
        default_symbols = [f"{b}/{quote_currency}" for b in meme_bases 
                          if f"{b}/{quote_currency}" in available_symbols]
    elif preset == "Layer 1s":
        l1_bases = ["BTC", "ETH", "SOL", "ADA", "AVAX", "DOT", "NEAR", "APT", 
                   "SUI", "SEI", "TIA", "ALGO", "FTM", "EOS", "KAS"]
        default_symbols = [f"{b}/{quote_currency}" for b in l1_bases 
                        if f"{b}/{quote_currency}" in available_symbols]
    elif preset == "DeFi tokens":
        defi_bases = ["UNI", "AAVE", "COMP", "MKR", "CRV", "SUSHI", "1INCH", 
                     "YFI", "SNX", "LDO", "DYDX", "BAL", "KNC"]
        default_symbols = [f"{b}/{quote_currency}" for b in defi_bases 
                        if f"{b}/{quote_currency}" in available_symbols]
    else:
        default_symbols = available_symbols[:10]

    symbols = st.multiselect("Symbols", available_symbols, default=default_symbols)

    st.subheader("Parameters")
    clip_usd = st.number_input("Clip size (USD)", 100, 200000, 5000, step=500)
    min_edge = st.number_input("Min net edge (bps)", 0.0, 500.0, 1.0, step=0.5)
    min_depth = st.number_input("Min book depth (USD)", 0, 100000, 1000, step=1000)

    st.subheader("🔧 Diagnostics")
    diagnostic_mode = st.toggle("Show ALL spreads (even negative)", value=False)
    show_raw_books = st.toggle("Show raw order books", value=False)
    enable_cross_quote = st.toggle("Enable cross-quote (stablecoin) arb", value=True)

    with st.expander("💰 Edit Taker Fees"):
        custom_fees = {}
        for ex in exchanges:
            default_fee = DEFAULT_TAKER_FEES.get(ex, 0.002)
            custom_fees[ex] = st.number_input(
                f"{ex}", 0.0, 0.1, default_fee, 
                format="%.4f", key=f"fee_{ex}"
            )

    st.divider()
    st.caption(
        "💡 **Finding no opportunities?** Enable "Show ALL spreads" to see gross "
        "spreads before fees. Most liquid pairs have near-zero spreads after taker fees."
    )

# --- Session state ---
if "pnl" not in st.session_state:
    st.session_state.pnl = 0.0
    st.session_state.trades = 0
    st.session_state.passes = 0
    st.session_state.trade_log = []
    st.session_state.best_spread_history = defaultdict(lambda: -9999)
    st.session_state.cross_quote_log = []

# --- Main UI ---
col_run, col_auto, col_test = st.columns([1, 2, 1])
run = col_run.button("🚀 Scan Now", type="primary", use_container_width=True)
auto = col_auto.toggle("Auto-rescan every 15s", value=False)
test_conn = col_test.button("Test Connections")

if test_conn:
    with st.spinner("Testing connections..."):
        test_clients = get_clients(tuple(sorted(exchanges)))
        st.success(f"Connected: {len(test_clients)}/{len(exchanges)}")
        for ex in exchanges:
            status = "✅" if ex in test_clients else "❌"
            st.write(f"{status} {ex}")

if run or auto:
    if not exchanges or not symbols:
        st.warning("Select at least one exchange and one symbol.")
        st.stop()

    total_calls = len(exchanges) * len(symbols)
    if total_calls > 500:
        st.warning(
            f"⚠️ {len(exchanges)} exchanges × {len(symbols)} symbols = {total_calls} API calls. "
            f"This may take 30–60s. Consider reducing count for faster scans."
        )

    clients = get_clients(tuple(sorted(exchanges)))
    if not clients:
        st.error("No exchanges connected. Test connections first.")
        st.stop()

    with st.spinner(f"Scanning {len(symbols)} symbols × {len(clients)} exchanges..."):
        t0 = time.time()
        quotes, health = scan(clients, symbols, clip_usd)
        quotes = [q for q in quotes if q.ask_depth_usd >= min_depth and q.bid_depth_usd >= min_depth]
        opps = find_opportunities(quotes, clip_usd, custom_fees, min_edge, diagnostic_mode)
        scan_time = time.time() - t0

    st.session_state.passes += 1

    for o in opps:
        key = o["symbol"]
        if o["net bps"] > st.session_state.best_spread_history[key]:
            st.session_state.best_spread_history[key] = o["net bps"]

    seen = set()
    trades_this_pass = 0
    for o in opps:
        if not diagnostic_mode and o["net bps"] >= min_edge:
            key = (o["symbol"], o["buy on"], o["sell on"])
            if key not in seen:
                seen.add(key)
                st.session_state.pnl += o["net pnl $"]
                st.session_state.trades += 1
                trades_this_pass += 1
                st.session_state.trade_log.append(o)

    # --- Metrics ---
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Passes", st.session_state.passes)
    m2.metric("Quotes", len(quotes))
    m3.metric("Trades/Pass", trades_this_pass)
    m4.metric("Total Trades", st.session_state.trades)
    m5.metric("Paper P&L", f"${st.session_state.pnl:,.2f}")

    st.caption(
        f"Scan: {scan_time:.1f}s | Quotes: {len(quotes)}/{len(exchanges)*len(symbols)} | "
        f"Exchanges up: {sum(1 for h in health.values() if h.ok > 0)}/{len(health)}"
    )

    # --- Exchange Health ---
    with st.expander("🏥 Exchange Health", expanded=True):
        h_data = []
        for ex, h in health.items():
            total = h.ok + h.fail
            h_data.append({
                "exchange": ex, "ok": h.ok, "fail": h.fail,
                "rate": f"{h.ok/total*100:.0f}%" if total > 0 else "-",
                "latency": f"{h.avg_latency_ms:.0f}ms",
                "last ok": h.last_success, "last err": h.last_err
            })
        st.dataframe(pd.DataFrame(h_data), hide_index=True, use_container_width=True)

    # --- Opportunities ---
    st.subheader("📊 Cross-Exchange Arbitrage")
    if opps:
        df = pd.DataFrame(opps)

        def color_net(val):
            if val > 10: return "background-color: #1b5e20; color: white"
            elif val > 0: return "background-color: #c8e6c9"
            elif val > -10: return "background-color: #fff9c4"
            else: return "background-color: #ffcdd2"

        st.dataframe(
            df.style.applymap(color_net, subset=["net bps"]),
            hide_index=True, use_container_width=True
        )

        if diagnostic_mode:
            pos = sum(1 for o in opps if o["net bps"] > 0)
            st.caption(f"Positive: {pos}/{len(opps)} ({pos/len(opps)*100:.1f}%)")
    else:
        st.info("No quotes returned. Check health panel.")

    # --- Cross-Quote Arbitrage ---
    if enable_cross_quote:
        st.subheader("💱 Cross-Quote (Stablecoin) Arbitrage")
        st.caption(
            "Compares BTC/USDT vs BTC/USDC etc. across exchanges. "
            "Requires holding both stablecoins."
        )

        cross_opps = find_cross_quote_opportunities(quotes, custom_fees, min_edge)
        if cross_opps:
            st.dataframe(pd.DataFrame(cross_opps), hide_index=True, use_container_width=True)
            st.session_state.cross_quote_log.extend(cross_opps)
        else:
            st.info("No cross-quote opportunities found.")

    # --- Why no opportunities? ---
    if not diagnostic_mode and (not opps or all(o["net bps"] <= 0 for o in opps)):
        st.subheader("🤔 Why Can't I Find Opportunities?")

        st.markdown("""
        **This is normal.** Efficient crypto markets have extremely tight spreads. Here's why and what to do:

        **1. Fees exceed spreads**
        - Taker fees: 10–60 bps on most exchanges
        - Gross spreads on liquid pairs: 0–5 bps
        - **Result**: Net spread is almost always negative

        **2. What actually works:**
        - **Exotic pairs**: PEPE, WIF, BONK, SATS, FLOKI (wider spreads, more inefficiency)
        - **Tier-2 exchanges**: LBank, WhiteBit, DigiFinex, XT (less efficient pricing)
        - **Cross-quote**: USDT vs USDC pairs (stablecoin premium arbitrage)
        - **Smaller clip sizes**: $500–$1,000 (larger sizes exhaust thin books)
        - **Lower min edge**: Try 0.0 bps to see everything

        **3. Quick fixes:**
        - Enable **"Show ALL spreads"** → see gross spreads before fees
        - Select **"Meme coins"** preset → scan exotic pairs
        - Add **LBank, WhiteBit, DigiFinex** → less efficient venues
        - Enable **cross-quote arbitrage** → stablecoin premium capture
        - Reduce clip to **$500** → avoid exhausting book depth
        """)

        if st.session_state.best_spread_history:
            hist = [(k, v) for k, v in st.session_state.best_spread_history.items() if v > -9999]
            if hist:
                st.markdown("**Best spreads seen this session:**")
                hist_df = pd.DataFrame(
                    sorted(hist, key=lambda x: x[1], reverse=True)[:10],
                    columns=["symbol", "best net bps"]
                )
                st.dataframe(hist_df, hide_index=True, use_container_width=True)

    # --- Raw Order Books ---
    if show_raw_books and quotes:
        st.subheader("📖 Raw Order Books")
        by_sym = defaultdict(list)
        for q in quotes:
            by_sym[q.symbol].append(q)

        for sym, qs in sorted(by_sym.items())[:10]:
            with st.expander(f"{sym}"):
                book_data = []
                for q in qs:
                    spread = (q.best_ask - q.best_bid) / q.best_ask * 10000 if q.best_ask else 0
                    book_data.append({
                        "exchange": q.exchange,
                        "best bid": q.best_bid,
                        "best ask": q.best_ask,
                        "spread bps": round(spread, 2),
                        "bid depth": f"${q.bid_depth_usd:,.0f}",
                        "ask depth": f"${q.ask_depth_usd:,.0f}",
                        "latency": f"{q.latency_ms:.0f}ms"
                    })
                st.dataframe(pd.DataFrame(book_data), hide_index=True, use_container_width=True)

    # --- Trade Log ---
    if st.session_state.trade_log:
        st.subheader("📜 Paper Trade Log")
        log_df = pd.DataFrame(st.session_state.trade_log)
        st.dataframe(log_df.tail(50), hide_index=True, use_container_width=True)

        c_dl, c_clr = st.columns([1, 5])
        with c_dl:
            st.download_button("Download CSV", log_df.to_csv(index=False).encode(), 
                              "arb_trades.csv", "text/csv")
        with c_clr:
            if st.button("Clear log"):
                st.session_state.trade_log = []
                st.session_state.pnl = 0.0
                st.session_state.trades = 0
                st.session_state.cross_quote_log = []
                st.rerun()

    if auto:
        time.sleep(15)
        st.rerun()

else:
    st.info("Press **Scan Now** to run, or enable auto-rescan.")
