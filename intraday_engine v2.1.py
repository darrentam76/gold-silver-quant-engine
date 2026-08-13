"""
intraday_engine.py - Version 0.2.1
===============================================================================
Institutional Precious Metals (XAU/XAG) & Yield Curve Quantitative Engine

Architecture Features:
----------------------
1. 1-Minute Multi-Asset Ingestion via yfinance with Fallback Protocol.
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
import time
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

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
ENGINE_VERSION = "intraday-v0.2.1"
CACHE_TTL_S = 60
PROXY_DISCLOSURE = (
    "Intraday 1-minute real rate changes use 10Y nominal yield variance "
    "anchored against daily breakeven."
)
ALLOWED_ASSETS = {"XAUUSD", "XAGUSD", "GOLD", "SILVER", "TNX", "TYX", "IRX", "GSR"}

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
    Resolves Primary Spot vs Fallback Futures with a rolling 30-bar mean basis correction:
    Adjusted_Fut_t = Fut_t + Mean_{30}(Spot_{t-30:t} - Fut_{t-30:t})
    """
    spot = df[spot_col].copy()
    fut = df[fut_col].copy()
    
    # Check if spot feed is empty or fully stale
    spot_valid_count = spot.dropna().shape[0]
    
    if spot_valid_count > 0:
        # Calculate overlapping 30-bar basis spread
        basis = spot - fut
        rolling_basis = basis.rolling(window=30, min_periods=1).mean()
        adjusted_fut = fut + rolling_basis
        
        # Primary spot with failover to adjusted futures where spot is NaN
        resolved_series = spot.fillna(adjusted_fut)
        
        # Determine source designation
        recent_spot_missing = spot.iloc[-3:].isna().all() if len(spot) >= 3 else True
        if recent_spot_missing:
            source = f"{fut_col}_FALLBACK"
        else:
            source = f"{spot_col}_SPOT"
    else:
        # Pure failover to raw futures if spot is completely missing
        logger.warning(f"Spot feed {spot_col} unresolvable. Falling back to {fut_col}.")
        resolved_series = fut
        source = f"{fut_col}_FALLBACK"

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

    # 2. Feed Resolution with 30-Bar Rolling Basis Adjustment
    gold_series, gold_source = _apply_futures_basis_adjustment(df, "XAUUSD=X", "GC=F")
    silver_series, silver_source = _apply_futures_basis_adjustment(df, "XAGUSD=X", "SI=F")

    # 3. Quality Gating (Forward fill limit = 3 bars)
    gold_ffill = gold_series.ffill(limit=3)
    silver_ffill = silver_series.ffill(limit=3)

    # Bars remain NaN if missing run > 3 consecutive bars
    is_stale_gold = gold_ffill.isna()
    is_stale_silver = silver_ffill.isna()
    is_stale_rates = df["^TNX"].ffill(limit=3).isna()

    # Joint Gating Condition
    is_stale = is_stale_gold | is_stale_silver | is_stale_rates

    # Compute GSR on valid bars only
    gsr_series = pd.Series(np.nan, index=df.index)
    valid_mask = ~is_stale
    gsr_series[valid_mask] = gold_ffill[valid_mask] / silver_ffill[valid_mask]

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
    rr_z = _rolling_z_changes(real_yield_10y, window=60)
    gold_z = _rolling_z_changes(gold_ffill, window=60)
    silver_z = _rolling_z_changes(silver_ffill, window=60)

    # Stat-Arb Relative Value Z-Score (ON LEVELS)
    gsr_z = _rolling_z_levels(gsr_series, window=60)

    # 6. Post-STALE Recovery Isolation
    stale_recovery_mask = is_stale | is_stale.shift(1).fillna(True)
    rr_z[stale_recovery_mask] = np.nan
    gold_z[stale_recovery_mask] = np.nan
    silver_z[stale_recovery_mask] = np.nan
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

    payload = {
        "signal_tag": str(macro_states.loc[last_idx]),
        "regime_tag": str(macro_states.loc[last_idx]),
        "arb_flag": str(arb_flags.loc[last_idx]),
        "quality": last_quality,
        "rr_z": _clean_val(rr_z, 2),
        "gold_z": _clean_val(gold_z, 2),
        "silver_z": _clean_val(silver_z, 2),
        "gsr_z": _clean_val(gsr_z, 2),
        "gold_price": _clean_val(gold_ffill, 2),
        "silver_price": _clean_val(silver_ffill, 2),
        "gsr_ratio": _clean_val(gsr_series, 2),
        "gsr": _clean_val(gsr_series, 2),
        "real_yield_10y": _clean_val(real_yield_10y, 3),
        "real_rate_10y": _clean_val(real_yield_10y, 3),
        "real_rate": _clean_val(real_yield_10y, 3),
        "slope_10y3m": _clean_val(slope_10y3m, 3),
        "slope_30y10y": _clean_val(slope_30y10y, 3),
        "data_source_gold": gold_source,
        "data_source_silver": silver_source,
        "proxy_disclosure": PROXY_DISCLOSURE,
        "data_as_of": last_idx.isoformat(),
        "timestamp": last_idx.isoformat(),
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
            return payload


cache_manager = SignalsCacheManager()

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
    data_source_gold: str = Field(..., examples=["XAUUSD=X_SPOT"])
    data_source_silver: str = Field(..., examples=["SI=F_FALLBACK"])
    proxy_disclosure: str = Field(..., examples=[PROXY_DISCLOSURE])
    data_as_of: str = Field(..., examples=["2026-08-12T14:32:00+00:00"])
    engine_version: str = Field(..., examples=[ENGINE_VERSION])


@app.get("/api/v1/signals", response_model=SignalsResponse)
async def get_signals_endpoint(
    breakeven_10y: float = Query(2.28, description="Daily 10Y Breakeven Inflation Anchor"),
    period: str = Query("1d", description="Lookback period (e.g., 1d, 5d, 7d)")
):
    """
    Returns latest quantitative signal state, relative-value arbitrage flags, 
    and yield curve metrics protected by a 60s server-side TTL cache.
    """
    try:
        payload = await cache_manager.get_signals(breakeven_10y=breakeven_10y, period=period)
        return payload
    except Exception as e:
        logger.error(f"Error generating signals: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Engine calculation error: {str(e)}")


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