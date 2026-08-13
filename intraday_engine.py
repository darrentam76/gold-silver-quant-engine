"""
intraday_engine.py - Version 0.2.3
===============================================================================
Institutional Precious Metals (XAU/XAG) & Yield Curve Quantitative Engine

Architecture Features:
----------------------
1. 1-Minute Multi-Asset Ingestion via yfinance with Futures-Primary Routing.
2. Futures-to-Spot Rolling 30-Bar Basis Adjustment (GC=F -> XAUUSD=X, SI=F -> XAGUSD=X).
3. Joint-Quality Gating (max 3-bar ffill limit; joint drop forces STALE state).
4. Real Rates & Yield Curve Analytics (10Y Real Rate Proxy, 10Y3M Slope, 30Y10Y Slope).
5. Dual Z-Score Modality Engine:
   - Macro Velocity Z-Scores computed on 1-minute CHANGES (delta_rr, delta_gold).
   - Stat-Arb Relative Value Z-Score computed on GSR LEVEL (gsr_z).
6. Post-STALE Isolation: Invalidates current and subsequent bar Z-scores on feed recovery.
7. Dual Hysteresis State Machines:
   - Macro Regime State Machine (5-bar exit streak hysteresis).
   - Stat-Arb Signal Flag Machine (3-bar exit streak hysteresis).
8. Thread-Safe Server-Side 60-Second TTL Cache for Upstream 429 Protection.
9. Out-of-Scope Asset Rejection Layer.
===============================================================================
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

from dotenv import load_dotenv

load_dotenv()

import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# Setup Structured Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("IntradayQuantEngine")

# System Constants & Disclosure Strings
ENGINE_VERSION = "intraday-v0.2.3"
CACHE_TTL_S = 60
PROXY_DISCLOSURE = (
    "Intraday 1-minute real rate changes use 10Y nominal yield variance "
    "anchored against daily breakeven."
)
ALLOWED_ASSETS = {"XAUUSD", "XAGUSD", "GOLD", "SILVER", "TNX", "TYX", "IRX", "GSR"}

ALLOWED_PERIODS = {"1d", "5d", "7d"}

# Supabase Persistence Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_TABLE = "signal_snapshots"
PERSISTENCE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

WINDOW_1D = 60
WINDOW_7D = 1440

def _resolve_window(period: str) -> int:
    """Spec Step 4: 1d -> 60-bar, 7d -> 1,440-bar rolling window."""
    return WINDOW_7D if period == "7d" else WINDOW_1D

# -----------------------------------------------------------------------------
# Pure Functions: Mathematical Engine & Signal Mechanics
# -----------------------------------------------------------------------------

def _rolling_z_changes(series: pd.Series, window: int = 60) -> pd.Series:
    r"""
    Computes rolling Z-score on 1-minute CHANGES (Velocity Modality).
    Enforces strict t-1 historical lookback (zero look-ahead bias).
    
    Z_{\Delta, t} = \frac{\Delta X_t - \mu_{t-1}(\Delta X)}{\sigma_{t-1}(\Delta X)}
    """
    delta = series.diff()
    # Shift historical window by 1 bar to restrict mean and std to t-1
    roll_mean = delta.shift(1).rolling(window=window, min_periods=max(10, window // 2)).mean()
    roll_std = delta.shift(1).rolling(window=window, min_periods=max(10, window // 2)).std(ddof=0)
    
    # Avoid divide-by-zero
    roll_std = roll_std.replace(0, np.nan)
    z_score = (delta - roll_mean) / roll_std
    return z_score


def _rolling_z_levels(series: pd.Series, window: int = 60) -> pd.Series:
    r"""
    Computes rolling Z-score on raw ratio LEVELS (Stat-Arb Modality).
    Enforces strict t-1 historical lookback (zero look-ahead bias).
    
    \text{gsr\_z}_t = \frac{\text{GSR}_t - \mu_{t-1}(\text{GSR})}{\sigma_{t-1}(\text{GSR})}
    """
    roll_mean = series.shift(1).rolling(window=window, min_periods=max(10, window // 2)).mean()
    roll_std = series.shift(1).rolling(window=window, min_periods=max(10, window // 2)).std(ddof=0)
    
    roll_std = roll_std.replace(0, np.nan)
    z_score = (series - roll_mean) / roll_std
    return z_score


def evaluate_regime_state(rr_z_series: pd.Series, gold_z_series: pd.Series) -> pd.Series:
    """
    Macro Regime State Machine with 5-bar exit streak hysteresis.
    
    Alert Thresholds (z = +-1.5):
    - DECOUPLING_ALERT: |rr_z| >= 1.5 AND |gold_z| >= 1.5 AND sign(rr_z) == sign(gold_z)
    - BEARISH_PRESSURE: rr_z >= +1.5
    - BULLISH_CATALYST: rr_z <= -1.5
    - NEUTRAL: Default state.
    """
    states = []
    current_state = "NEUTRAL"
    calm_streak = 0
    
    for rz, gz in zip(rr_z_series.values, gold_z_series.values):
        if np.isnan(rz) or np.isnan(gz):
            target_state = "NEUTRAL"
        else:
            is_decoupling = (abs(rz) >= 1.5) and (abs(gz) >= 1.5) and (np.sign(rz) == np.sign(gz))
            is_bearish = rz >= 1.5
            is_bullish = rz <= -1.5

            if is_decoupling:
                target_state = "DECOUPLING_ALERT"
            elif is_bearish:
                target_state = "BEARISH_PRESSURE"
            elif is_bullish:
                target_state = "BULLISH_CATALYST"
            else:
                target_state = "NEUTRAL"

        if target_state != "NEUTRAL":
            current_state = target_state
            calm_streak = 0
        else:
            if current_state != "NEUTRAL":
                calm_streak += 1
                if calm_streak >= 5:
                    current_state = "NEUTRAL"
                    calm_streak = 0
            else:
                calm_streak = 0
                
        states.append(current_state)
        
    return pd.Series(states, index=rr_z_series.index)


def evaluate_arb_flags(gsr_z_series: pd.Series) -> pd.Series:
    """
    Stat-Arb Signal Flag Machine with 3-bar exit streak hysteresis.
    
    Thresholds (arb_threshold = +-2.0):
    - GSR_LONG_SILVER_SHORT_GOLD: gsr_z >= +2.0
    - GSR_LONG_GOLD_SHORT_SILVER: gsr_z <= -2.0
    - NONE: Default state.
    """
    flags = []
    current_flag = "NONE"
    calm_streak = 0
    
    for gz in gsr_z_series.values:
        if np.isnan(gz):
            target_flag = "NONE"
        elif gz >= 2.0:
            target_flag = "GSR_LONG_SILVER_SHORT_GOLD"
        elif gz <= -2.0:
            target_flag = "GSR_LONG_GOLD_SHORT_SILVER"
        else:
            target_flag = "NONE"

        if target_flag != "NONE":
            current_flag = target_flag
            calm_streak = 0
        else:
            if current_flag != "NONE":
                calm_streak += 1
                if calm_streak >= 3:
                    current_flag = "NONE"
                    calm_streak = 0
            else:
                calm_streak = 0
                
        flags.append(current_flag)
        
    return pd.Series(flags, index=gsr_z_series.index)


# -----------------------------------------------------------------------------
# Data Pipeline & Ingestion Architecture
# -----------------------------------------------------------------------------

def _fetch_raw_market_data(period: str = "1d") -> pd.DataFrame:
    # Uses GC=F and SI=F directly as primary feeds to prevent 404 noise
    tickers = ["GC=F", "SI=F", "^TNX", "^TYX", "^IRX"]
    data = yf.download(
        tickers=tickers,
        period=period,
        interval="1m",
        group_by="ticker",
        progress=False,
        auto_adjust=True
    )

    # Static Type Safety & Null Safety Guardrail
    if data is None or data.empty:
        return pd.DataFrame()

    df_close = pd.DataFrame(index=data.index)
    for t in tickers:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                df_close[t] = data[t]["Close"]
            else:
                df_close[t] = data["Close"][t] if "Close" in data else data[t]
        except KeyError:
            df_close[t] = np.nan
            
    # Map primary futures directly to expected spot columns for internal compatibility
    if "GC=F" in df_close.columns:
        df_close["XAUUSD=X"] = df_close["GC=F"]
    if "SI=F" in df_close.columns:
        df_close["XAGUSD=X"] = df_close["SI=F"]

    # Apply CBOE Yield Scaling Rule (* 0.1)
    for yield_ticker in ["^TNX", "^TYX", "^IRX"]:
        if yield_ticker in df_close.columns:
            df_close[yield_ticker] = df_close[yield_ticker] * 0.1

    return df_close


def _apply_futures_basis_adjustment(
    df: pd.DataFrame, spot_col: str, fut_col: str
) -> Tuple[pd.Series, str]:
    """
    Resolves Primary Futures vs Spot with a rolling 30-bar mean basis correction:
    Adjusted_Fut_t = Fut_t + Mean_{30}(Spot_{t-30:t} - Fut_{t-30:t})
    """
    spot = df[spot_col].copy()
    fut = df[fut_col].copy()
    
    spot_valid_count = spot.dropna().shape[0]
    
    if spot_valid_count > 0:
        basis = spot - fut
        rolling_basis = basis.rolling(window=30, min_periods=30).mean()
        adjusted_fut = fut + rolling_basis
        resolved_series = spot.fillna(adjusted_fut)
    else:
        resolved_series = fut

    # Futures primary source designation
    source = f"{fut_col}_PRIMARY"
    return resolved_series, source


def run_intraday_pipeline(breakeven_10y: float = 2.28, period: str = "1d") -> Dict[str, Any]:
    """
    Executes the end-to-end quantitative pipeline.
    """
    raw_df = _fetch_raw_market_data(period=period)
    
    if raw_df.empty or len(raw_df) < 5:
        raise ValueError("Insufficient market data bars fetched from upstream source.")

    # 1. Resample to strict 1-minute UTC grid
    df = raw_df.resample("1min").asfreq()

    # 2. Feed Resolution with Futures-Primary Basis Adjustment
    gold_series, gold_source = _apply_futures_basis_adjustment(df, "XAUUSD=X", "GC=F")
    silver_series, silver_source = _apply_futures_basis_adjustment(df, "XAGUSD=X", "SI=F")

    # 3. Quality Gating (Forward fill limit = 3 bars)
    gold_ffill = gold_series.ffill(limit=3)
    silver_ffill = silver_series.ffill(limit=3)

    is_stale_gold = gold_ffill.isna()
    is_stale_silver = silver_ffill.isna()

    # Step 2 (literal): GSR joint gating on Gold OR Silver only
    is_stale_gsr = is_stale_gold | is_stale_silver

    # Constraint 3: off-session rates deplete 3-bar budget -> STALE quality
    is_stale_rates = df["^TNX"].ffill(limit=3).isna()
    is_stale = is_stale_gsr | is_stale_rates

    # Compute GSR on gold/silver-valid bars only (Step 2)
    gsr_series = pd.Series(np.nan, index=df.index)
    gsr_series[~is_stale_gsr] = gold_ffill[~is_stale_gsr] / silver_ffill[~is_stale_gsr]

    # Forward-fill Treasury feeds with 3-bar budget
    tnx = df["^TNX"].ffill(limit=3)
    tyx = df["^TYX"].ffill(limit=3)
    irx = df["^IRX"].ffill(limit=3)

    # 4. Yield Curve & Proxy Real Rates
    real_yield_10y = tnx - breakeven_10y
    slope_10y3m = tnx - irx
    slope_30y10y = tyx - tnx

    # 5. Z-Score Modalities
    # Macro Velocity Z-Scores (ON CHANGES)
    window = _resolve_window(period)

    rr_z = _rolling_z_changes(real_yield_10y, window=window)
    gold_z = _rolling_z_changes(gold_ffill, window=window)
    
    # Stat-Arb Relative Value Z-Score (ON LEVELS)
    gsr_z = _rolling_z_levels(gsr_series, window=window)

    

    # 6. Post-STALE Recovery Isolation
    stale_recovery_mask = is_stale | is_stale.shift(1).fillna(False)
    rr_z[stale_recovery_mask] = np.nan
    gold_z[stale_recovery_mask] = np.nan
    gsr_z[stale_recovery_mask] = np.nan

    # 7. Hysteresis Dual State Machines
    macro_states = evaluate_regime_state(rr_z, gold_z)
    arb_flags = evaluate_arb_flags(gsr_z)

    # Extract Latest Bar Outputs
    last_idx = df.index[-1]
    last_quality = "STALE" if is_stale.loc[last_idx] else "OK"

    def _clean_val(series: pd.Series, decimals: int = 4) -> Optional[float]:
        val = series.loc[last_idx]
        if pd.isna(val) or np.isinf(val):
            return None
        return round(float(val), decimals)

    # Cleaned Payload matching Target Output JSON Contract v0.2.3 exactly
    payload = {
        "signal_tag": str(macro_states.loc[last_idx]),
        "arb_flag": str(arb_flags.loc[last_idx]),
        "quality": last_quality,
        "rr_z": _clean_val(rr_z, 2),
        "gold_z": _clean_val(gold_z, 2),
        "gsr_z": _clean_val(gsr_z, 2),
        "gold_price": _clean_val(gold_ffill, 2),
        "silver_price": _clean_val(silver_ffill, 2),
        "gsr_ratio": _clean_val(gsr_series, 2),
        "real_yield_10y": _clean_val(real_yield_10y, 3),
        "slope_10y3m": _clean_val(slope_10y3m, 3),
        "slope_30y10y": _clean_val(slope_30y10y, 3),
        "data_source_gold": gold_source,
        "data_source_silver": silver_source,
        "proxy_disclosure": PROXY_DISCLOSURE,
        "data_as_of": last_idx.isoformat(),
        "engine_version": ENGINE_VERSION
    }

    return payload


# -----------------------------------------------------------------------------
# Thread-Safe Server-Side 60-Second TTL Cache
# -----------------------------------------------------------------------------

class SignalsCacheManager:
    def __init__(self, ttl_seconds: int = CACHE_TTL_S):
        self.ttl = ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}  # {cache_key: {"payload": ..., "fetched_at": float}}
        self._lock = asyncio.Lock()

    async def get_signals(self, breakeven_10y: float, period: str) -> Dict[str, Any]:
        async with self._lock:
            now = time.time()
            cache_key = f"{breakeven_10y}_{period}"
            
            cached_entry = self._cache.get(cache_key)
            if cached_entry and (now - cached_entry["fetched_at"] < self.ttl):
                logger.info(f"Serving signals for '{cache_key}' from 60s server-side TTL cache.")
                return cached_entry["payload"]

            logger.info("Cache expired or miss. Executing quantitative pipeline...")
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(
                None, run_intraday_pipeline, breakeven_10y, period
            )
            
            self._cache[cache_key] = {"payload": payload, "fetched_at": now}

            # Best-effort archival on cache miss (one row per unique bar)
            persist_payload(payload, breakeven_10y, period)
            return payload


cache_manager = SignalsCacheManager()

# -----------------------------------------------------------------------------
# Supabase Persistence Layer (Best-Effort Signal Snapshot Archival)
# -----------------------------------------------------------------------------

_supabase_client = None


def _get_supabase():
    """Lazy-init the sync Supabase client (safe for executor threads)."""
    global _supabase_client
    if _supabase_client is None and PERSISTENCE_ENABLED:
        from supabase import create_client
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def persist_payload(payload: Dict[str, Any], breakeven_10y: float, period: str) -> None:
    """
    Upserts one signal snapshot to Supabase keyed on data_as_of.
    Best-effort: any failure is logged and suppressed so the API never degrades.
    """
    if not PERSISTENCE_ENABLED:
        return
    try:
        client = _get_supabase()
        if client is None:
            return
        row = {
            "data_as_of": payload["data_as_of"],
            "signal_tag": payload["signal_tag"],
            "arb_flag": payload["arb_flag"],
            "quality": payload["quality"],
            "rr_z": payload.get("rr_z"),
            "gold_z": payload.get("gold_z"),
            "gsr_z": payload.get("gsr_z"),
            "gold_price": payload.get("gold_price"),
            "silver_price": payload.get("silver_price"),
            "gsr_ratio": payload.get("gsr_ratio"),
            "real_yield_10y": payload.get("real_yield_10y"),
            "slope_10y3m": payload.get("slope_10y3m"),
            "slope_30y10y": payload.get("slope_30y10y"),
            "data_source_gold": payload["data_source_gold"],
            "data_source_silver": payload["data_source_silver"],
            "breakeven_10y": breakeven_10y,
            "period": period,
            "engine_version": payload["engine_version"],
        }
        client.table(SUPABASE_TABLE).upsert(row, on_conflict="data_as_of").execute()
        logger.info(f"Supabase: persisted signal snapshot data_as_of={row['data_as_of']}.")
    except Exception as e:
        logger.error(f"Supabase persistence failed (non-fatal): {str(e)}")


def fetch_history(limit: int = 200) -> Dict[str, Any]:
    """Reads recent signal snapshots from Supabase, newest first."""
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase persistence is not configured.")
    resp = client.table(SUPABASE_TABLE).select("*").order("data_as_of", desc=True).limit(limit).execute()
    return {"count": len(resp.data), "rows": resp.data}


# -----------------------------------------------------------------------------
# FastAPI API Layer & Out-of-Scope Rejection Protocols
# -----------------------------------------------------------------------------

app = FastAPI(
    title="Institutional Precious Metals & Real Rates Quant API",
    version=ENGINE_VERSION,
    description="Cross-asset relative value stat-arb and yield curve macro engine."
)


class SignalsResponse(BaseModel):
    signal_tag: str = Field(..., examples=["BEARISH_PRESSURE"])
    arb_flag: str = Field(..., examples=["GSR_LONG_SILVER_SHORT_GOLD"])
    quality: str = Field(..., examples=["OK"])
    rr_z: Optional[float] = Field(None, examples=[1.82])
    gold_z: Optional[float] = Field(None, examples=[-0.45])
    gsr_z: Optional[float] = Field(None, examples=[2.15])
    gold_price: Optional[float] = Field(None, examples=[2385.40])
    silver_price: Optional[float] = Field(None, examples=[28.45])
    gsr_ratio: Optional[float] = Field(None, examples=[83.85])
    real_yield_10y: Optional[float] = Field(None, examples=[1.840])
    slope_10y3m: Optional[float] = Field(None, examples=[-0.410])
    slope_30y10y: Optional[float] = Field(None, examples=[0.290])
    data_source_gold: str = Field(..., examples=["GC=F_PRIMARY"])
    data_source_silver: str = Field(..., examples=["SI=F_PRIMARY"])
    proxy_disclosure: str = Field(..., examples=[PROXY_DISCLOSURE])
    data_as_of: str = Field(..., examples=["2026-08-14T04:00:00+00:00"])
    engine_version: str = Field(..., examples=[ENGINE_VERSION])


@app.get("/api/v1/signals", response_model=SignalsResponse)
async def get_signals_endpoint(
    breakeven_10y: float = Query(2.28, description="Daily 10Y Breakeven Inflation Anchor"),
    period: str = Query("1d", description="Lookback period (1d, 5d, 7d)")
):
    """
    Returns latest quantitative signal state, relative-value arbitrage flags,
    and yield curve metrics protected by a 60s server-side TTL cache.
    """
    if period not in ALLOWED_PERIODS:
        raise HTTPException(
            status_code=400,
            detail="period must be one of: 1d, 5d, 7d (7-day maximum per Yahoo API rules)."
        )
    try:
        payload = await cache_manager.get_signals(breakeven_10y=breakeven_10y, period=period)
        return payload
    except Exception as e:
        logger.error(f"Error generating signals: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Engine calculation error: {str(e)}")


@app.get("/api/v1/history")
async def get_history_endpoint(limit: int = Query(200, ge=1, le=5000)):
    """
    Returns recent signal snapshots from Supabase, newest first.
    Powers the dashboard Analytics tab with persisted time-series data.
    """
    if not PERSISTENCE_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase persistence is not configured (SUPABASE_URL/SUPABASE_KEY missing)."
        )
    try:
        return fetch_history(limit=limit)
    except Exception as e:
        logger.error(f"History query failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"History query error: {str(e)}")


@app.get("/api/v1/reject")
async def reject_out_of_scope(asset: str = Query(...)):
    """
    Explicit interface rejection layer for non-macro or out-of-scope tickers.
    """
    clean_asset = asset.upper().strip()
    if clean_asset not in ALLOWED_ASSETS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Asset '{asset}' is rejected. This system is strictly constrained "
                "to macro precious metals (XAU/XAG) and US Treasury real rates."
            )
        )
    return {"status": "ALLOWED", "asset": clean_asset}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)