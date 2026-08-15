import os
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import streamlit as st

GMT8 = timezone(timedelta(hours=8))


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

# -----------------------------------------------------------------------------
# 1. Page Configuration & Theme (colors/fonts live in .streamlit/config.toml)
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Gold, Silver & Real Rates Engine",
    page_icon=":material/show_chart:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------------------------------------------------------
# 2. Sidebar Controls
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title(":material/tune: Engine controls")

    default_backend = os.getenv("BACKEND_URL", "http://localhost:8000")
    backend_url = st.text_input("Backend API URL", value=default_backend).rstrip("/")

    st.divider()
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

    st.divider()
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

timestamp = to_gmt8(signals_data.get("data_as_of"))
quality = signals_data.get("quality", "UNKNOWN")
data_source_gold = signals_data.get("data_source_gold", "N/A")
data_source_silver = signals_data.get("data_source_silver", "N/A")

live_color = "green" if quality == "OK" else "red"
st.markdown(
    f":{live_color}-badge[● LIVE FEED] "
    f"Quality: `{quality}` · Gold feed: `{data_source_gold}` · "
    f"Silver feed: `{data_source_silver}` · Updated (GMT+8): `{timestamp}`"
)

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
    real_rate = signals_data.get("real_yield_10y") or 0.0
    rr_z = signals_data.get("rr_z") or 0.0
    gold_price = signals_data.get("gold_price") or 0.0
    gold_z = signals_data.get("gold_z") or 0.0
    silver_price = signals_data.get("silver_price") or 0.0
    gsr_ratio = signals_data.get("gsr_ratio") or 0.0
    gsr_z = signals_data.get("gsr_z") or 0.0

    with st.container(horizontal=True):
        st.metric(
            "10Y real rate",
            f"{real_rate:.2f}%",
            delta=f"{rr_z:.2f}σ Z",
            delta_color="inverse",
            border=True,
        )
        st.metric(
            "Gold spot",
            f"${gold_price:,.2f}",
            delta=f"{gold_z:.2f}σ Z",
            border=True,
        )
        st.metric("Silver spot", f"${silver_price:,.2f}", border=True)
        st.metric(
            "Gold/silver ratio",
            f"{gsr_ratio:.2f}",
            delta=f"{gsr_z:.2f}σ Z",
            delta_color="inverse",
            border=True,
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
                signals_data.get("rr_z", 0.0),
                signals_data.get("gold_z", 0.0),
                signals_data.get("gsr_z", 0.0),
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
    fig_z.update_layout(height=320, margin=dict(l=20, r=20, t=30, b=20))
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
    freshness = d.get("freshness_min", {})
    st.caption(
        "Feed freshness (min since last tick): "
        + " · ".join(
            f"{k.upper()}: `{v}`" if v is not None else f"{k.upper()}: n/a"
            for k, v in freshness.items()
        )
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
    fig_yearly.update_layout(height=780, showlegend=True, margin=dict(l=20, r=20, t=40, b=20))
    st.plotly_chart(fig_yearly, width="stretch")


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
            fig_hist.update_layout(
                height=650,
                showlegend=True,
                margin=dict(l=20, r=20, t=40, b=20),
            )
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
