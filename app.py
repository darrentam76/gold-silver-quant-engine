import os
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import streamlit as st

GMT8 = timezone(timedelta(hours=8))

UI_BUILD = "held-rate"  # bump on each UI change so deployments are verifiable at a glance


def to_gmt8(iso_str: str) -> str:
    """Convert an ISO-8601 timestamp to GMT+8 wall-clock, formatted as YYYY-MM-DD HH:MM:SS."""
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return str(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(GMT8).strftime("%Y-%m-%d %H:%M:%S")


def _style_figure(fig: go.Figure, height: int = 400, margin_top: int = 48, hovermode: str = "x unified") -> go.Figure:
    """Apply the dashboard's consistent, low-noise chart style."""
    fig.update_layout(
        height=height,
        font=dict(family="Inter, system-ui, sans-serif", size=13, color="#CBD5E1"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=margin_top, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, bgcolor="rgba(0,0,0,0)"),
        hovermode=hovermode,
        hoverlabel=dict(bgcolor="rgba(15,23,42,0.95)", bordercolor="rgba(148,163,184,0.30)", font=dict(color="#F1F5F9")),
    )
    grid = "rgba(148, 163, 184, 0.10)"
    axis = "rgba(148, 163, 184, 0.22)"
    fig.update_xaxes(showgrid=True, gridcolor=grid, linecolor=axis, zerolinecolor=axis)
    fig.update_yaxes(showgrid=True, gridcolor=grid, linecolor=axis, zerolinecolor=axis)
    return fig

# -----------------------------------------------------------------------------
# 1. Page Configuration & Theme (colors/fonts live in .streamlit/config.toml)
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Gold, Silver & Real Rates Engine",
    page_icon=":material/show_chart:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Design system: typography, translucent surfaces, responsive feedback.
st.html(
    """
<style>
/* Typography — optical sizing, negative tracking & tight leading on headings */
.stApp {
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
}
.stApp h1 { letter-spacing: -0.025em; line-height: 1.08; }
.stApp h2 { letter-spacing: -0.02em; line-height: 1.15; }
.stApp h3 { letter-spacing: -0.015em; line-height: 1.2; }

/* Metric cards — translucent surface, soft depth (shadow over hard border) */
div[data-testid="stMetric"] {
    background: rgba(148, 163, 184, 0.06);
    backdrop-filter: blur(8px) saturate(140%);
    border: 1px solid rgba(148, 163, 184, 0.16) !important;
    border-radius: 10px;
    box-shadow: 0 1px 2px rgba(2, 6, 23, 0.40), 0 10px 28px -14px rgba(0, 0, 0, 0.55);
    transition: transform 160ms cubic-bezier(0.23, 1, 0.32, 1), box-shadow 160ms cubic-bezier(0.23, 1, 0.32, 1);
}
@media (hover: hover) and (pointer: fine) {
    div[data-testid="stMetric"]:hover {
        transform: translateY(-1px);
        box-shadow: 0 2px 4px rgba(2, 6, 23, 0.45), 0 14px 32px -14px rgba(0, 0, 0, 0.60);
    }
}

/* Buttons — instant press feedback (scale 0.97, <160ms ease-out) */
.stButton > button, .stDownloadButton > button {
    transition: transform 120ms cubic-bezier(0.23, 1, 0.32, 1), box-shadow 160ms ease-out, background-color 160ms ease-out;
    will-change: transform;
}
.stButton > button:active, .stDownloadButton > button:active {
    transform: scale(0.97);
}

/* Tabs — fast, subtle state change */
button[data-baseweb="tab"] {
    transition: background-color 160ms ease-out, color 160ms ease-out;
}

/* Sticky header — frosted glass edge instead of a hard divider */
[data-testid="stHeader"] {
    background: rgba(15, 23, 42, 0.72) !important;
    backdrop-filter: blur(16px) saturate(180%);
    box-shadow: none;
    border-bottom: none;
}

/* Reduced motion — keep color/opacity, drop movement */
@media (prefers-reduced-motion: reduce) {
    div[data-testid="stMetric"],
    .stButton > button, .stDownloadButton > button,
    button[data-baseweb="tab"] {
        transition: none !important;
        transform: none !important;
    }
}
</style>
"""
)

# -----------------------------------------------------------------------------
# 2. Sidebar Controls
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title(":material/tune: Engine controls")

    default_backend = os.getenv("BACKEND_URL", "http://localhost:8000")
    backend_url = st.text_input("Backend API URL", value=default_backend).rstrip("/")

    st.space("small")
    st.subheader("Model parameters")

    breakeven_input = st.number_input(
        "10Y breakeven inflation (%)",
        value=2.28,
        step=0.01,
        format="%.2f",
        help="Passed to the backend to calculate the real yield from the nominal 10Y yield.",
    )

    z_threshold = st.slider(
        "Reference threshold (display only)",
        min_value=0.5,
        max_value=3.0,
        value=1.5,
        step=0.1,
        help="Draws display-only reference lines on the Z-score chart.",
    )
    st.caption("Engine thresholds are fixed: macro ±1.5σ, stat-arb ±2.0σ.")

    st.space("small")
    if st.button(":material/refresh: Force refresh cache", type="primary", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    st.caption("Backend contract: `intraday-v0.2.4`")

# -----------------------------------------------------------------------------
# 3. API Data Fetching (Strict Fail-Fast Behavior)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def fetch_signals(url: str, breakeven: float) -> dict:
    """Fetch live signals from backend. Pure network call (no st.* calls inside cache)."""
    endpoint = f"{url}/api/v1/signals"
    params = {"breakeven_10y": breakeven}

    try:
        response = requests.get(endpoint, params=params, timeout=90)
        if response.status_code != 200:
            return {"error": True, "message": f"Backend API returned status code {response.status_code}: {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Failed to connect to backend at `{endpoint}`: {e}"}


def fetch_insights(url: str, payload: dict) -> dict:
    """Post signal state to LLM endpoint. Returns raw response or surfaces exact error."""
    endpoint = f"{url}/api/v1/insights"
    try:
        response = requests.post(endpoint, json=payload, timeout=15)
        if response.status_code != 200:
            return {"error": True, "message": f"Backend API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}


def fetch_history(url: str, limit: int = 500, trading_date: str = None) -> dict:
    """Fetch persisted signal snapshots from backend /api/v1/history (Supabase-backed)."""
    endpoint = f"{url}/api/v1/history"
    params = {"limit": limit}
    if trading_date:
        params["trading_date"] = trading_date
    try:
        response = requests.get(endpoint, params=params, timeout=90)
        if response.status_code != 200:
            return {"error": True, "message": f"History API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}


def fetch_daily_summary(url: str, limit: int = 30) -> dict:
    """Fetch per-trading-day aggregates from backend /api/v1/history/daily."""
    endpoint = f"{url}/api/v1/history/daily"
    try:
        response = requests.get(endpoint, params={"limit": limit}, timeout=90)
        if response.status_code != 200:
            return {"error": True, "message": f"Daily API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}


def fetch_live_quotes(url: str, breakeven: float) -> dict:
    """Fetch live futures quotes from backend /api/v1/live. NOT cached — wants fresh ticks."""
    endpoint = f"{url}/api/v1/live"
    try:
        response = requests.get(endpoint, params={"breakeven_10y": breakeven}, timeout=15)
        if response.status_code != 200:
            return {"error": True, "message": f"Live API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}


@st.cache_data(ttl=1800)
def fetch_yearly_history(url: str, period: str = "1y") -> dict:
    """Fetch daily bars for futures/yields/GSR from backend /api/v1/live/yearly."""
    endpoint = f"{url}/api/v1/live/yearly"
    try:
        response = requests.get(endpoint, params={"period": period}, timeout=60)
        if response.status_code != 200:
            return {"error": True, "message": f"Yearly API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}


def fetch_price_history(url: str, symbol: str = None, limit: int = 1000) -> dict:
    """Fetch archived daily OHLCV rows from backend /api/v1/price-history (Supabase-backed)."""
    endpoint = f"{url}/api/v1/price-history"
    params = {"limit": limit}
    if symbol:
        params["symbol"] = symbol
    try:
        response = requests.get(endpoint, params=params, timeout=90)
        if response.status_code != 200:
            return {"error": True, "message": f"Archive API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}


def backfill_price_history(url: str, days: int = 400) -> dict:
    """Trigger a Supabase archive backfill of daily OHLCV via POST."""
    endpoint = f"{url}/api/v1/price-history/backfill"
    try:
        response = requests.post(endpoint, params={"days": days}, timeout=180)
        if response.status_code != 200:
            return {"error": True, "message": f"Backfill API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}


# Fetch core payload and guard against None
signals_result = fetch_signals(backend_url, breakeven_input) or {"error": True, "message": "No response from fetch_signals."}
if signals_result["error"]:
    st.error(signals_result["message"], icon=":material/error:")
    st.stop()
signals_data = signals_result["data"]

# -----------------------------------------------------------------------------
# 4. Header & Status Banner
# -----------------------------------------------------------------------------
st.title("Gold, Silver & Real Rates Engine")
st.caption(f"Quantitative XAU/XAG & yield-curve trading dashboard · backend contract `{signals_data.get('engine_version', 'unknown')}` · ui build `{UI_BUILD}`")

timestamp = to_gmt8(signals_data.get("data_as_of"))
quality = signals_data.get("quality", "UNKNOWN")
data_source_gold = signals_data.get("data_source_gold", "N/A")
data_source_silver = signals_data.get("data_source_silver", "N/A")

feed_badge = f":{'green' if quality == 'OK' else 'red'}-badge[● LIVE FEED]"
quality_badge = f":{'green' if quality == 'OK' else 'orange'}-badge[Quality: {quality}]"
st.markdown(
    f"{feed_badge} {quality_badge} · Gold feed: `{data_source_gold}` · "
    f"Silver feed: `{data_source_silver}` · Updated (GMT+8): `{timestamp}`"
)
st.space("small")

# -----------------------------------------------------------------------------
# 5. Tabbed Navigation
# -----------------------------------------------------------------------------
tab_regime, tab_live, tab_yearly, tab_analytics, tab_llm = st.tabs(
    [
        ":material/query_stats: Regime & signals",
        ":material/sensors: Live prices",
        ":material/area_chart: 1Y history",
        ":material/table_chart: Analytics",
        ":material/chat: AI commentary",
    ]
)


def _regime_color(tag: str) -> str:
    return {
        "NEUTRAL": "gray",
        "BULLISH_CATALYST": "green",
        "BEARISH_PRESSURE": "red",
        "DECOUPLING_ALERT": "orange",
    }.get(tag, "gray")


def _flag_color(flag: str) -> str:
    return {
        "NONE": "gray",
        "GSR_LONG_SILVER_SHORT_GOLD": "violet",
        "GSR_LONG_GOLD_SHORT_SILVER": "blue",
    }.get(flag, "gray")


# --- TAB 1: REGIME & SIGNALS ---
with tab_regime:
    real_rate = signals_data.get("real_yield_10y")
    real_rate_held = signals_data.get("real_yield_10y_held")
    real_rate_held_from = signals_data.get("real_rate_held_from")
    is_held = real_rate is None and real_rate_held is not None
    real_rate = real_rate if real_rate is not None else real_rate_held
    real_rate = real_rate if real_rate is not None else 0.0
    rr_z = signals_data.get("rr_z")
    gold_price = signals_data.get("gold_price") or 0.0
    gold_z = signals_data.get("gold_z")
    silver_price = signals_data.get("silver_price") or 0.0
    gsr_ratio = signals_data.get("gsr_ratio") or 0.0
    gsr_z = signals_data.get("gsr_z")

    def _z(delta_z: Optional[float]) -> str:
        return "n/a σ" if delta_z is None else f"{delta_z:.2f}σ Z"

    with st.container(horizontal=True):
        st.metric(
            "10Y real rate",
            f"{real_rate:.2f}%",
            delta=_z(rr_z),
            delta_color="inverse",
            border=True,
        )
        st.metric(
            "Gold spot",
            f"${gold_price:,.2f}",
            delta=_z(gold_z),
            border=True,
        )
        st.metric("Silver spot", f"${silver_price:,.2f}", border=True)
        st.metric(
            "Gold/silver ratio",
            f"{gsr_ratio:.2f}",
            delta=_z(gsr_z),
            delta_color="inverse",
            border=True,
        )

    if is_held:
        try:
            held_dt = datetime.fromisoformat(real_rate_held_from.replace("Z", "+00:00"))
            held_txt = held_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            held_txt = str(real_rate_held_from)
        st.caption(
            f"Real rate **held** from last Treasury close ({held_txt}) — "
            "^TNX off-session. Z-scores return when cash Treasuries resume."
        )

    st.space("small")
    st.subheader("Signal state")

    regime_tag = signals_data.get("signal_tag", "NEUTRAL")
    arb_flag = signals_data.get("arb_flag", "NONE")

    state_col1, state_col2 = st.columns(2)
    with state_col1:
        st.markdown("**Macro regime**")
        st.badge(regime_tag, color=_regime_color(regime_tag), icon=":material/speed:")
    with state_col2:
        st.markdown("**Stat-arb flag**")
        st.badge(arb_flag, color=_flag_color(arb_flag), icon=":material/balance:")

    st.subheader("Z-score velocity monitor")

    z_df = pd.DataFrame(
        {
            "Metric": ["Real rate Z (rr_z)", "Gold Z (gold_z)", "GSR Z (gsr_z)"],
            "Z-Score": [
                rr_z if rr_z is not None else float("nan"),
                gold_z if gold_z is not None else float("nan"),
                gsr_z if gsr_z is not None else float("nan"),
            ],
        }
    )

    fig_z = px.bar(
        z_df,
        x="Z-Score",
        y="Metric",
        orientation="h",
        color="Z-Score",
        color_continuous_scale=["#F87171", "#334155", "#34D399"],
        range_x=[-4.0, 4.0],
    )
    fig_z.add_vline(
        x=z_threshold,
        line_dash="dash",
        line_color="#F59E0B",
        annotation_text=f"+{z_threshold:.1f}σ",
    )
    fig_z.add_vline(
        x=-z_threshold,
        line_dash="dash",
        line_color="#F59E0B",
        annotation_text=f"-{z_threshold:.1f}σ",
    )
    fig_z.update_yaxes(autorange="reversed")
    fig_z.update_coloraxes(showscale=False)
    _style_figure(fig_z, height=320, margin_top=30, hovermode="y")
    st.plotly_chart(fig_z, width="stretch")

# --- TAB 2: LIVE PRICES (auto-refresh, independent of STALE gate) ---
@st.fragment(run_every=10)
def live_prices_panel():
    st.subheader("Live futures & rates terminal")
    st.caption("Auto-refreshes every 10s during trading hours. Gold = `GC=F`, Silver = `SI=F` (COMEX futures, not spot).")

    result = fetch_live_quotes(backend_url, breakeven_input)
    if result["error"]:
        st.error(result["message"], icon=":material/error:")
        return
    d = result["data"]

    open_color = "green" if d.get("market_open") else "red"
    st.markdown(
        f":{open_color}-badge[{'● MARKET LIVE' if d.get('market_open') else '○ MARKET CLOSED'}] "
        f"Updated (GMT+8): `{to_gmt8(d.get('data_as_of'))}`"
    )

    def _fmt(v, dec=2):
        return f"{v:,.{dec}f}" if v is not None else "—"

    gold = d.get("gold_futures")
    silver = d.get("silver_futures")
    gsr = d.get("gsr_ratio")
    tnx = d.get("us10y_yield")
    tyx = d.get("us30y_yield")
    irx = d.get("us3m_yield")

    with st.container(horizontal=True):
        st.metric("Gold futures (GC=F)", f"${_fmt(gold)}", border=True)
        st.metric("Silver futures (SI=F)", f"${_fmt(silver)}", border=True)
        st.metric("Gold/silver ratio", f"{_fmt(gsr)}", border=True)
        st.metric("10Y yield", f"{_fmt(tnx, 3)}%", border=True)
        st.metric("30Y yield", f"{_fmt(tyx, 3)}%", border=True)
        st.metric("3M yield", f"{_fmt(irx, 3)}%", border=True)

    st.space("small")
    st.subheader("Derived curve metrics")
    with st.container(horizontal=True):
        st.metric("10Y real yield (proxy)", f"{_fmt(d.get('real_yield_10y'), 3)}%", border=True)
        st.metric("Slope 10Y-3M", f"{_fmt(d.get('slope_10y3m'), 3)}%", border=True)
        st.metric("Slope 30Y-10Y", f"{_fmt(d.get('slope_30y10y'), 3)}%", border=True)

    st.space("small")
    staleness = d.get("staleness_min")
    staleness_txt = f"{staleness:.0f} min ago" if staleness is not None else "n/a"
    st.caption(
        f"**Last tick: {staleness_txt}** · Yahoo's 1m futures feed publishes bars a few minutes "
        "behind the market · this panel auto-refreshes every 10s"
    )


with tab_live:
    live_prices_panel()

# --- TAB 3: 1Y HISTORY ---
@st.fragment(run_every=600)
def yearly_history_panel():
    st.subheader("Past-year market history")
    st.caption("Daily bars from `GC=F`, `SI=F` and CBOE yields · times in GMT+8 · backend cached 1h.")

    result = fetch_yearly_history(backend_url, period="1y")
    if result["error"]:
        st.error(result["message"], icon=":material/error:")
        return
    rows = result["data"].get("rows", [])
    if not rows:
        st.warning("No yearly history returned from the backend.", icon=":material/warning:")
        return

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(GMT8)

    st.subheader("Latest values")
    last = df.iloc[-1]
    with st.container(horizontal=True):
        st.metric("Gold (GC=F)", f"${last['gold']:,.2f}", border=True)
        st.metric("Silver (SI=F)", f"${last['silver']:,.2f}", border=True)
        st.metric("Gold/silver ratio", f"{last['gsr']:.2f}", border=True)
        st.metric("10Y yield", f"{last['us10y']:.3f}%", border=True)
        st.metric("30Y yield", f"{last['us30y']:.3f}%", border=True)
        st.metric("3M yield", f"{last['us3m']:.3f}%", border=True)

    st.subheader("Trends")
    fig_yearly = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.45, 0.2, 0.35],
        subplot_titles=("Gold & silver futures", "Gold/silver ratio", "Treasury yields"),
        specs=[[{"secondary_y": True}], [{}], [{}]],
    )
    fig_yearly.add_trace(
        go.Scatter(x=df["date"], y=df["gold"], name="Gold (GC=F)", line=dict(color="#F59E0B")),
        row=1, col=1, secondary_y=False,
    )
    fig_yearly.add_trace(
        go.Scatter(x=df["date"], y=df["silver"], name="Silver (SI=F)", line=dict(color="#94A3B8")),
        row=1, col=1, secondary_y=True,
    )
    fig_yearly.add_trace(
        go.Scatter(x=df["date"], y=df["gsr"], name="GSR", line=dict(color="#34D399")),
        row=2, col=1,
    )
    fig_yearly.add_trace(
        go.Scatter(x=df["date"], y=df["us10y"], name="10Y", line=dict(color="#60A5FA")),
        row=3, col=1,
    )
    fig_yearly.add_trace(
        go.Scatter(x=df["date"], y=df["us30y"], name="30Y", line=dict(color="#A78BFA")),
        row=3, col=1,
    )
    fig_yearly.add_trace(
        go.Scatter(x=df["date"], y=df["us3m"], name="3M", line=dict(color="#FBBF24")),
        row=3, col=1,
    )
    _style_figure(fig_yearly, height=780)
    st.plotly_chart(fig_yearly, width="stretch")

    st.space("medium")
    st.subheader("Supabase price archive")

    archive = fetch_price_history(backend_url, limit=1)
    if archive["error"]:
        st.warning(f"Archive status unavailable: {archive['message']}", icon=":material/info:")
        archive_total = 0
    else:
        archive_total = archive["data"].get("total", 0)

    st.caption(
        f"**{archive_total:,} archived OHLCV rows** in `price_history_daily`. Daily bars are "
        "mirrored automatically on every fresh 1Y fetch (upsert), so the archive stays current "
        "as long as the dashboard is visited. Use the button to seed or refresh it explicitly."
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(":material/archive: Backfill archive (400 days)", width="stretch"):
            with st.spinner("Downloading & archiving daily bars (may take ~1 min)..."):
                res = backfill_price_history(backend_url, days=400)
            if res["error"]:
                st.error(res["message"], icon=":material/error:")
            else:
                st.success(
                    f"Archived {res['data']['rows_written']:,} rows across "
                    f"{len(res['data']['symbols'])} symbols up to {res['data']['latest_date']}.",
                    icon=":material/check_circle:",
                )
                st.cache_data.clear()
                st.rerun()
    with col_b:
        if st.button(":material/download: Load archive as CSV", width="stretch"):
            rows_all = fetch_price_history(backend_url, limit=5000)
            if rows_all["error"]:
                st.error(rows_all["message"], icon=":material/error:")
            else:
                pdf = pd.DataFrame(rows_all["data"]["rows"])
                st.download_button(
                    "Save price_history_daily.csv",
                    data=pdf.to_csv(index=False),
                    file_name="price_history_daily.csv",
                    mime="text/csv",
                    width="stretch",
                )


with tab_yearly:
    yearly_history_panel()

# --- TAB 4: ANALYTICS ---
with tab_analytics:
    st.subheader("Historical series (Supabase persistence)")

    daily_result = fetch_daily_summary(backend_url)

    if not daily_result["error"] and daily_result["data"].get("days"):
        days = daily_result["data"]["days"]

        st.caption("**Per-trading-day summary (GMT+8)**")
        summary_df = pd.DataFrame(
            [
                {
                    "Trading Date": d["trading_date_gmt8"],
                    "Snapshots": d["count"],
                    "OK": d["ok"],
                    "STALE": d["stale"],
                    "Top Signal": next(iter(d["signal_tags"]), "NEUTRAL"),
                    "Top Flag": next(iter(d["arb_flags"]), "NONE"),
                }
                for d in days
            ]
        )
        st.dataframe(summary_df, width="stretch", hide_index=True)

        date_options = ["All"] + [d["trading_date_gmt8"] for d in days]
        selected_date = st.selectbox("Trading date (GMT+8)", options=date_options, index=0)
        date_param = None if selected_date == "All" else selected_date

        history_result = fetch_history(backend_url, trading_date=date_param, limit=500)

        if not history_result["error"] and history_result["data"].get("rows"):
            df_hist = pd.DataFrame(history_result["data"]["rows"])
            df_hist = df_hist.sort_values("data_as_of").reset_index(drop=True)
            df_hist["data_as_of"] = pd.to_datetime(df_hist["data_as_of"], utc=True).dt.tz_convert(GMT8)

            st.caption(
                f"**{len(df_hist)} snapshots**"
                + (f" on **{selected_date}**" if selected_date != "All" else " (all dates)")
                + " · times in GMT+8 · newest at bottom"
            )

            fig_hist = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.08,
                row_heights=[0.6, 0.4],
                subplot_titles=("Gold price vs 10Y real rate", "GSR ratio vs GSR Z-score"),
                specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
            )
            fig_hist.add_trace(
                go.Scatter(x=df_hist["data_as_of"], y=df_hist["gold_price"], name="Gold price ($)", line=dict(color="#F59E0B")),
                secondary_y=False,
                row=1,
                col=1,
            )
            fig_hist.add_trace(
                go.Scatter(x=df_hist["data_as_of"], y=df_hist["real_yield_10y"], name="10Y real rate (%)", line=dict(color="#60A5FA")),
                secondary_y=True,
                row=1,
                col=1,
            )
            fig_hist.add_trace(
                go.Scatter(x=df_hist["data_as_of"], y=df_hist["gsr_ratio"], name="Gold/silver ratio", line=dict(color="#34D399")),
                secondary_y=False,
                row=2,
                col=1,
            )
            fig_hist.add_trace(
                go.Scatter(x=df_hist["data_as_of"], y=df_hist["gsr_z"], name="GSR Z-score", line=dict(color="#A78BFA")),
                secondary_y=True,
                row=2,
                col=1,
            )
            _style_figure(fig_hist, height=650)
            st.plotly_chart(fig_hist, width="stretch")
        else:
            st.warning("No snapshots found for the selected date.", icon=":material/warning:")
    else:
        st.warning(
            "No persisted snapshots yet. Run the backend and hit `/api/v1/signals` to archive data, and confirm the `signal_snapshots` table exists in Supabase.",
            icon=":material/warning:",
        )

# --- TAB 5: AI COMMENTARY ---
with tab_llm:
    st.subheader("Executive LLM synthesis")
    st.caption("Generates real-time macro analysis based strictly on the current signal state payload.")

    if quality != "OK":
        st.warning(
            "Signal quality is currently `STALE`. Real-rate variance and Z-scores are null, "
            "so the payload is descriptive rather than predictive. DeepSeek analysis is "
            "disabled until a fresh OK-quality bar populates.",
            icon=":material/warning:",
        )
    else:
        if st.button(":material/auto_awesome: Request DeepSeek analysis", type="primary"):
            with st.spinner("Transmitting signal state payload to DeepSeek LLM..."):
                result = fetch_insights(backend_url, signals_data)

                if result["error"]:
                    st.error(result["message"], icon=":material/error:")
                else:
                    insight_text = result["data"].get("insight", "No insight narrative returned in payload.")
                    with st.container(border=True):
                        st.markdown("### Executive synthesis")
                        st.markdown(insight_text)
