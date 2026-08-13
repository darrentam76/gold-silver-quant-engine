import os
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import streamlit as st  # <-- Must be imported before using 'st'

# -----------------------------------------------------------------------------
# 1. Page Configuration & Terminal Theme
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Gold, Silver & Real Rates Engine",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Dark Financial Terminal CSS
st.markdown("""
    <style>
        .stApp {
            background-color: #0E1117;
            font-family: 'Inter', -apple-system, monospace;
        }
        div[data-testid="stMetric"] {
            background-color: #1E222D;
            border: 1px solid #2E3440;
            padding: 16px;
            border-radius: 8px;
        }
        div[data-testid="stPlotlyChart"] {
            background-color: #1E222D;
            border: 1px solid #2E3440;
            border-radius: 8px;
            padding: 8px;
        }
        .status-badge {
            font-family: monospace;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.85rem;
            font-weight: 600;
        }
        .status-live { background-color: #064E3B; color: #34D399; border: 1px solid #059669; }
        .status-error { background-color: #7F1D1D; color: #FCA5A5; border: 1px solid #DC2626; }
    </style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 2. Sidebar Controls
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title("⚡ Engine Controls")
    
    # Backend URL configuration (Replit Secret with localhost fallback)
    default_backend = os.getenv("BACKEND_URL", "http://localhost:8000")
    backend_url = st.text_input("Backend API URL", value=default_backend).rstrip("/")
    
    st.markdown("---")
    st.subheader("Model Parameters")
    
    # Wired Controls
    breakeven_input = st.number_input(
        "10Y Breakeven Inflation (%)", 
        value=2.28, 
        step=0.01, 
        format="%.2f",
        help="Passed to backend to calculate real yield from nominal 10Y yield"
    )
    
    z_threshold = st.slider(
        "Z-Score Threshold (σ)", 
        min_value=1.0, 
        max_value=3.0, 
        value=2.0, 
        step=0.1,
        help="Sets reference trigger lines on Z-score charts"
    )
    
    st.markdown("---")
    if st.button("🔄 Force Refresh Cache", width="stretch"):
        st.cache_data.clear()
        st.rerun()

# -----------------------------------------------------------------------------
# 3. API Data Fetching (Strict Fail-Fast Behavior)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def fetch_signals(url: str, breakeven: float) -> dict:
    """Fetch live signals from backend. Raises on failure to avoid stale/synthetic data."""
    endpoint = f"{url}/api/v1/signals"
    # Align param name with backend API: `breakeven_10y`
    params = {"breakeven_10y": breakeven}
    
    try:
        response = requests.get(endpoint, params=params, timeout=5)
        if response.status_code != 200:
            st.error(f"Backend API returned status code {response.status_code}: {response.text}")
            st.stop()
        return response.json()
    except requests.exceptions.RequestException as e:
        st.error(f"Failed to connect to backend at `{endpoint}`: {e}")
        st.stop()

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

def fetch_history(url: str, limit: int = 500) -> dict:
    """Fetch persisted signal snapshots from backend /api/v1/history (Supabase-backed)."""
    endpoint = f"{url}/api/v1/history"
    try:
        response = requests.get(endpoint, params={"limit": limit}, timeout=10)
        if response.status_code != 200:
            return {"error": True, "message": f"History API Error ({response.status_code}): {response.text}"}
        return {"error": False, "data": response.json()}
    except requests.exceptions.RequestException as e:
        return {"error": True, "message": f"Connection Error: Could not reach backend at `{endpoint}` ({e})"}

# Fetch core payload and guard against None
signals_data = fetch_signals(backend_url, breakeven_input) or {}

# -----------------------------------------------------------------------------
# 4. Header & Status Banner (Spec v0.2.1 Compliance)
# -----------------------------------------------------------------------------
st.title("Gold, Silver & Real Rates Engine")

# Extract status contract fields safely
timestamp = signals_data.get("timestamp", "N/A")
quality = signals_data.get("quality", "UNKNOWN")
data_source_gold = signals_data.get("data_source_gold", "N/A")
data_source_silver = signals_data.get("data_source_silver", "N/A")

st.markdown(
    f"""
    <div style="display: flex; gap: 15px; align-items: center; margin-bottom: 20px; flex-wrap: wrap;">
        <span class="status-badge status-live">● LIVE FEED</span>
        <span><b>Quality:</b> <code>{quality}</code></span>
        <span>|</span>
        <span><b>Gold Feed:</b> {data_source_gold}</span>
        <span>|</span>
        <span><b>Silver Feed:</b> {data_source_silver}</span>
        <span>|</span>
        <span><b>Timestamp:</b> {timestamp}</span>
    </div>
    """,
    unsafe_allow_html=True
)

# -----------------------------------------------------------------------------
# 5. Tabbed Navigation
# -----------------------------------------------------------------------------
tab_regime, tab_analytics, tab_llm = st.tabs(["Regime & Signals", "Analytics", "AI Commentary"])

# --- TAB 1: REGIME & SIGNALS ---
with tab_regime:
    col1, col2, col3, col4 = st.columns(4)
    
    real_rate = signals_data.get("real_rate_10y", 0.0)
    gold_price = signals_data.get("gold_price", 0.0)
    silver_price = signals_data.get("silver_price", 0.0)
    gsr = signals_data.get("gsr", 0.0)
    
    col1.metric("10Y Real Rate", f"{real_rate:.2f}%", delta=f"{signals_data.get('rr_z', 0.0):.2f}σ Z", delta_color="inverse")
    col2.metric("Gold Spot", f"${gold_price:,.2f}", delta=f"{signals_data.get('gold_z', 0.0):.2f}σ Z")
    col3.metric("Silver Spot", f"${silver_price:,.2f}", delta=f"{signals_data.get('silver_z', 0.0):.2f}σ Z")
    col4.metric("Gold/Silver Ratio", f"{gsr:.2f}", delta=f"{signals_data.get('gsr_z', 0.0):.2f}σ Z")
    
    st.markdown("---")
    
    # Regime & Signal Banners
    regime_tag = signals_data.get("signal_tag", signals_data.get("regime_tag", "NEUTRAL"))
    arb_flag = signals_data.get("arb_flag", "NO_ARBITRAGE")
    
    r_col, a_col = st.columns(2)
    with r_col:
        st.info(f"**Macro Regime Tag:** `{regime_tag}`")
    with a_col:
        st.success(f"**Stat-Arb Flag:** `{arb_flag}`")
        
    st.subheader("Z-Score Velocity Monitor")
    
    # Wired Z-Score Horizontal Bar Chart
    z_df = pd.DataFrame({
        "Metric": ["Real Rate Z (rr_z)", "Gold Z (gold_z)", "Silver Z (silver_z)", "GSR Z (gsr_z)"],
        "Z-Score": [
            signals_data.get("rr_z", 0.0),
            signals_data.get("gold_z", 0.0),
            signals_data.get("silver_z", 0.0),
            signals_data.get("gsr_z", 0.0)
        ]
    })
    
    fig_z = px.bar(
        z_df,
        x="Z-Score",
        y="Metric",
        orientation="h",
        color="Z-Score",
        color_continuous_scale=["#EF4444", "#1E222D", "#10B981"],
        range_x=[-4.0, 4.0]
    )
    
    # Dynamic Threshold Reference Lines
    fig_z.add_vline(x=z_threshold, line_dash="dash", line_color="#F59E0B", annotation_text=f"+{z_threshold:.1f}σ Threshold")
    fig_z.add_vline(x=-z_threshold, line_dash="dash", line_color="#F59E0B", annotation_text=f"-{z_threshold:.1f}σ Threshold")
    
    fig_z.update_layout(
        template="plotly_dark",
        paper_bgcolor="#1E222D",
        plot_bgcolor="#0E1117",
        height=320,
        margin=dict(l=20, r=20, t=30, b=20)
    )
    st.plotly_chart(fig_z, width="stretch")

# --- TAB 2: ANALYTICS ---
with tab_analytics:
    st.subheader("Historical Series (Supabase Persistence)")

    history_result = fetch_history(backend_url)

    if not history_result["error"] and history_result["data"].get("rows"):
        df_hist = pd.DataFrame(history_result["data"]["rows"])
        df_hist = df_hist.sort_values("data_as_of").reset_index(drop=True)
        df_hist["data_as_of"] = pd.to_datetime(df_hist["data_as_of"])

        st.caption(f"**{len(df_hist)} persisted snapshots** from Supabase · newest at bottom")

        fig_hist = make_subplots(
            rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
            row_heights=[0.6, 0.4],
            subplot_titles=("Gold Price vs 10Y Real Rate", "GSR Ratio vs GSR Z-Score"),
            specs=[[{"secondary_y": True}], [{"secondary_y": True}]]
        )
        fig_hist.add_trace(
            go.Scatter(x=df_hist["data_as_of"], y=df_hist["gold_price"], name="Gold Price ($)", line=dict(color="#F59E0B")),
            secondary_y=False, row=1, col=1
        )
        fig_hist.add_trace(
            go.Scatter(x=df_hist["data_as_of"], y=df_hist["real_yield_10y"], name="10Y Real Rate (%)", line=dict(color="#3B82F6")),
            secondary_y=True, row=1, col=1
        )
        fig_hist.add_trace(
            go.Scatter(x=df_hist["data_as_of"], y=df_hist["gsr_ratio"], name="Gold/Silver Ratio", line=dict(color="#34D399")),
            secondary_y=False, row=2, col=1
        )
        fig_hist.add_trace(
            go.Scatter(x=df_hist["data_as_of"], y=df_hist["gsr_z"], name="GSR Z-Score", line=dict(color="#F472B6")),
            secondary_y=True, row=2, col=1
        )
        fig_hist.update_layout(
            template="plotly_dark", paper_bgcolor="#1E222D", plot_bgcolor="#0E1117",
            height=650, showlegend=True, margin=dict(l=20, r=20, t=40, b=20)
        )
        st.plotly_chart(fig_hist, width="stretch")

        quality_counts = df_hist["quality"].value_counts().to_dict()
        st.caption("Quality mix: " + ", ".join(f"`{k}`: {v}" for k, v in quality_counts.items()))
    else:
        st.warning("⚠️ No persisted snapshots yet. Run the backend and hit `/api/v1/signals` to archive data, and confirm the `signal_snapshots` table exists in Supabase.")

# --- TAB 3: AI COMMENTARY ---
with tab_llm:
    st.subheader("Executive LLM Synthesis")
    st.write("Generate real-time macro analysis based strictly on the current signal state payload.")
    
    if st.button("🚀 Request DeepSeek Analysis", type="primary"):
        with st.spinner("Transmitting signal state payload to DeepSeek LLM..."):
            result = fetch_insights(backend_url, signals_data)
            
            if result["error"]:
                st.error(result["message"])
            else:
                insight_text = result["data"].get("insight", "No insight narrative returned in payload.")
                st.markdown("### Executive Synthesis")
                st.markdown(f">{insight_text}")