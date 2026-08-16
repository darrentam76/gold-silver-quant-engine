"""
intraday_engine.py - Version 0.2.4
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
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Tuple, Optional

from dotenv import load_dotenv

load_dotenv()

import numpy as np
import pandas as pd
import requests
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
ENGINE_VERSION = "intraday-v0.2.4"
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
PRICE_HISTORY_TABLE = "price_history_daily"
PERSISTENCE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

# DeepSeek Executive Synthesis Configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT_S = 30

WINDOW_1D = 60
WINDOW_7D = 1440

# Model & Threshold Constants (Spec Steps 4-5)
REGIME_Z_THRESHOLD = 1.5    # macro regime state machine alert threshold (z = +-1.5)
REGIME_EXIT_STREAK = 5      # calm bars before macro state reverts to NEUTRAL
ARB_Z_THRESHOLD = 2.0       # stat-arb signal flag threshold (z = +-2.0)
ARB_EXIT_STREAK = 3         # calm bars before arbitrage flag reverts to NONE
FFILL_LIMIT = 3             # max consecutive forward-filled bars before STALE
BASIS_WINDOW = 30           # rolling mean window for futures/spot basis adjustment
MIN_INGEST_BARS = 5         # minimum bars required to run the pipeline
MIN_Z_PERIODS = 10          # floor for rolling Z-score min_periods
HISTORY_SCAN_LIMIT = 20000  # row cap when aggregating daily summaries
MARKET_TICKERS = ["GC=F", "SI=F", "^TNX", "^TYX", "^IRX"]
CBOE_YIELD_SCALE = 0.1      # legacy constant retained for docs; scaling is now auto-detected

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
    roll_mean = delta.shift(1).rolling(window=window, min_periods=max(MIN_Z_PERIODS, window // 2)).mean()
    roll_std = delta.shift(1).rolling(window=window, min_periods=max(MIN_Z_PERIODS, window // 2)).std(ddof=0)
    
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
    roll_mean = series.shift(1).rolling(window=window, min_periods=max(MIN_Z_PERIODS, window // 2)).mean()
    roll_std = series.shift(1).rolling(window=window, min_periods=max(MIN_Z_PERIODS, window // 2)).std(ddof=0)
    
    roll_std = roll_std.replace(0, np.nan)
    z_score = (series - roll_mean) / roll_std
    return z_score


def evaluate_regime_state(rr_z_series: pd.Series, gold_z_series: pd.Series) -> pd.Series:
    """
    Macro Regime State Machine with 5-bar exit streak hysteresis.
    
    Alert Thresholds (z = +-REGIME_Z_THRESHOLD):
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
            is_decoupling = (abs(rz) >= REGIME_Z_THRESHOLD) and (abs(gz) >= REGIME_Z_THRESHOLD) and (np.sign(rz) == np.sign(gz))
            is_bearish = rz >= REGIME_Z_THRESHOLD
            is_bullish = rz <= -REGIME_Z_THRESHOLD

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
                if calm_streak >= REGIME_EXIT_STREAK:
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
        elif gz >= ARB_Z_THRESHOLD:
            target_flag = "GSR_LONG_SILVER_SHORT_GOLD"
        elif gz <= -ARB_Z_THRESHOLD:
            target_flag = "GSR_LONG_GOLD_SHORT_SILVER"
        else:
            target_flag = "NONE"

        if target_flag != "NONE":
            current_flag = target_flag
            calm_streak = 0
        else:
            if current_flag != "NONE":
                calm_streak += 1
                if calm_streak >= ARB_EXIT_STREAK:
                    current_flag = "NONE"
                    calm_streak = 0
            else:
                calm_streak = 0
                
        flags.append(current_flag)
        
    return pd.Series(flags, index=gsr_z_series.index)


# -----------------------------------------------------------------------------
# Data Pipeline & Ingestion Architecture
# -----------------------------------------------------------------------------

def _normalize_yield_series(values: pd.Series) -> pd.Series:
    """Normalize CBOE yield feeds to percent.

    yfinance behavior varies by version: historically ``^TNX``/``^TYX``/``^IRX``
    were returned CBOE-scaled x10 (42.50 = 4.250%); yfinance >=1.0 returns the
    percent directly (4.25). Heuristic: a latest value above 20 is an x10 form.
    """
    latest = values.dropna()
    if latest.empty:
        return values
    if float(latest.iloc[-1]) > 20.0:
        return values * CBOE_YIELD_SCALE
    return values


def _fetch_raw_market_data(period: str = "1d") -> pd.DataFrame:
    # Uses GC=F and SI=F directly as primary feeds to prevent 404 noise
    tickers = MARKET_TICKERS
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

    # Apply CBOE Yield Scaling Rule (auto-detect: yfinance >=1.0 returns yields
    # in percent directly; older versions returned the CBOE x10 form 42.50 = 4.25%)
    for yield_ticker in ["^TNX", "^TYX", "^IRX"]:
        if yield_ticker in df_close.columns:
            df_close[yield_ticker] = _normalize_yield_series(df_close[yield_ticker])

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
        rolling_basis = basis.rolling(window=BASIS_WINDOW, min_periods=BASIS_WINDOW).mean()
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
    
    if raw_df.empty or len(raw_df) < MIN_INGEST_BARS:
        raise ValueError("Insufficient market data bars fetched from upstream source.")

    # 1. Resample to strict 1-minute UTC grid
    df = raw_df.resample("1min").asfreq()

    # 2. Feed Resolution with Futures-Primary Basis Adjustment
    gold_series, gold_source = _apply_futures_basis_adjustment(df, "XAUUSD=X", "GC=F")
    silver_series, silver_source = _apply_futures_basis_adjustment(df, "XAGUSD=X", "SI=F")

    # 3. Quality Gating (Forward fill limit = 3 bars)
    gold_ffill = gold_series.ffill(limit=FFILL_LIMIT)
    silver_ffill = silver_series.ffill(limit=FFILL_LIMIT)

    is_stale_gold = gold_ffill.isna()
    is_stale_silver = silver_ffill.isna()

    # Step 2 (literal): GSR joint gating on Gold OR Silver only
    is_stale_gsr = is_stale_gold | is_stale_silver

    # Constraint 3: off-session rates deplete 3-bar budget -> STALE quality
    is_stale_rates = df["^TNX"].ffill(limit=FFILL_LIMIT).isna()
    is_stale = is_stale_gsr | is_stale_rates

    # Compute GSR on gold/silver-valid bars only (Step 2)
    gsr_series = pd.Series(np.nan, index=df.index)
    gsr_series[~is_stale_gsr] = gold_ffill[~is_stale_gsr] / silver_ffill[~is_stale_gsr]

    # Forward-fill Treasury feeds with 3-bar budget
    tnx = df["^TNX"].ffill(limit=FFILL_LIMIT)
    tyx = df["^TYX"].ffill(limit=FFILL_LIMIT)
    irx = df["^IRX"].ffill(limit=FFILL_LIMIT)

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

    # Cleaned Payload matching Target Output JSON Contract v0.2.4 exactly
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
# Live Futures Quote Layer (10-Second TTL, Independent of STALE Signal Gating)
# -----------------------------------------------------------------------------

LIVE_QUOTE_TTL_S = 10
LIVE_FRESH_MIN = 60  # gold/silver feed considered live if last tick < 60 min ago


def _futures_market_open(now_utc: pd.Timestamp) -> bool:
    """COMEX/Globex futures session in America/New_York wall-clock:
    opens Sunday 18:00 ET, closes Friday 17:00 ET, with a daily maintenance
    break 17:00-18:00 ET on Monday-Thursday."""
    try:
        et = now_utc.tz_convert("America/New_York")
    except Exception:
        et = now_utc.tz_convert("Etc/GMT+5")  # EST fallback, never raises
    dow = et.dayofweek          # 0=Mon .. 6=Sun
    minute = et.hour * 60 + et.minute
    if dow == 5:                # Saturday: closed
        return False
    if dow == 6:                # Sunday: opens 18:00 ET
        return minute >= 18 * 60
    if dow == 4:                # Friday: closes 17:00 ET
        return minute < 17 * 60
    return not (17 * 60 <= minute < 18 * 60)  # Mon-Thu daily break


def _fetch_live_quotes(breakeven_10y: float = 2.28) -> Dict[str, Any]:
    """
    Returns the latest futures prices (GC=F gold, SI=F silver) plus CBOE
    Treasury yields from the 1-minute feed, without the STALE signal gate.
    Also reports per-feed freshness (minutes since last tick) so the dashboard
    can show whether a feed is live, off-session, or frozen (weekend/holiday).
    """
    df = _fetch_raw_market_data(period="1d")
    if df.empty:
        raise ValueError("No market data available for live quotes.")

    df = df.dropna(how="all")
    if df.empty:
        raise ValueError("No live market data available for live quotes.")

    last = df.iloc[-1]
    latest_ts = df.index.max()

    def _clean(col: str, decimals: int = 4) -> Optional[float]:
        series = df[col].dropna()
        if series.empty:
            return None
        val = series.iloc[-1]
        if pd.isna(val) or np.isinf(val):
            return None
        return round(float(val), decimals)

    def _age_min(col: str) -> Optional[float]:
        series = df[col].dropna()
        if series.empty:
            return None
        ts = series.index[-1]
        if ts.tzinfo is None or latest_ts.tzinfo is None or ts.tzinfo == latest_ts.tzinfo:
            delta = latest_ts - ts
        else:
            delta = latest_ts.tz_convert(ts.tz) - ts
        return round(delta.total_seconds() / 60.0, 1)

    # Wall-clock staleness + futures session check (feed max timestamp can look
    # "fresh" even on a closed market, since it equals the last session close).
    now_utc = pd.Timestamp.now(tz="UTC")
    if latest_ts.tzinfo is None:
        latest_utc = latest_ts.tz_localize("UTC")
    else:
        latest_utc = latest_ts.tz_convert("UTC")
    age_now_min = (now_utc - latest_utc).total_seconds() / 60.0
    market_open = bool(age_now_min < LIVE_FRESH_MIN and _futures_market_open(now_utc))

    tnx = _clean("^TNX", 3)
    tyx = _clean("^TYX", 3)
    irx = _clean("^IRX", 3)
    gold = _clean("GC=F", 2)
    silver = _clean("SI=F", 2)

    gsr = round(gold / silver, 2) if gold and silver else None
    real_yield_10y = round(tnx - breakeven_10y, 3) if tnx is not None else None
    slope_10y3m = round(tnx - irx, 3) if (tnx is not None and irx is not None) else None
    slope_30y10y = round(tyx - tnx, 3) if (tyx is not None and tnx is not None) else None

    gold_age = _age_min("GC=F")

    return {
        "gold_futures": gold,
        "silver_futures": silver,
        "gsr_ratio": gsr,
        "us10y_yield": tnx,
        "us30y_yield": tyx,
        "us3m_yield": irx,
        "real_yield_10y": real_yield_10y,
        "slope_10y3m": slope_10y3m,
        "slope_30y10y": slope_30y10y,
        "market_open": market_open,
        "freshness_min": {
            "gold": gold_age,
            "silver": _age_min("SI=F"),
            "us10y": _age_min("^TNX"),
            "us30y": _age_min("^TYX"),
            "us3m": _age_min("^IRX"),
        },
        "data_as_of": latest_ts.isoformat(),
        "engine_version": ENGINE_VERSION,
    }


class LiveQuotesManager:
    """Thread-safe 10-second TTL cache for the live futures quote endpoint."""

    def __init__(self, ttl_seconds: int = LIVE_QUOTE_TTL_S):
        self.ttl = ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_quotes(self, breakeven_10y: float) -> Dict[str, Any]:
        async with self._lock:
            now = time.time()
            cache_key = str(breakeven_10y)

            cached = self._cache.get(cache_key)
            if cached and (now - cached["fetched_at"] < self.ttl):
                return cached["payload"]

            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(None, _fetch_live_quotes, breakeven_10y)
            self._cache[cache_key] = {"payload": payload, "fetched_at": now}
            return payload


live_quotes_manager = LiveQuotesManager()

# -----------------------------------------------------------------------------
# Yearly History Layer (Daily Bars, 1-Hour TTL Cache)
# -----------------------------------------------------------------------------

YEARLY_HISTORY_TTL_S = 3600
ALLOWED_HISTORY_PERIODS = {"1y", "6mo", "3mo", "1mo"}

_last_daily_frame: Optional[pd.DataFrame] = None


def _download_daily_ohlcv(
    period: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> pd.DataFrame:
    """Downloads daily OHLCV for all MARKET_TICKERS (yields normalized, plus a
    computed GSR close) into a (ticker, field) MultiIndex frame.

    Accepts either a yfinance ``period`` token (1y/6mo/3mo/1mo) or explicit
    ``start``/``end`` datetimes for arbitrary-length backfills.
    """
    kwargs: Dict[str, Any] = {
        "tickers": MARKET_TICKERS,
        "interval": "1d",
        "group_by": "ticker",
        "progress": False,
        "auto_adjust": True,
    }
    if period:
        kwargs["period"] = period
    else:
        kwargs["start"] = start.strftime("%Y-%m-%d") if start else None
        kwargs["end"] = (end + timedelta(days=1)).strftime("%Y-%m-%d") if end else None

    data = yf.download(**kwargs)
    if data is None or data.empty:
        raise ValueError("No daily market history available.")

    columns = pd.MultiIndex.from_product(
        [MARKET_TICKERS, ["Open", "High", "Low", "Close", "Volume"]]
    )
    frame = pd.DataFrame(index=data.index, columns=columns, dtype=float)

    if isinstance(data.columns, pd.MultiIndex):
        for t in MARKET_TICKERS:
            for f in ["Open", "High", "Low", "Close", "Volume"]:
                try:
                    frame[(t, f)] = data[t][f].astype(float)
                except (KeyError, TypeError):
                    pass
    else:
        for f in ["Open", "High", "Low", "Close", "Volume"]:
            if f in data.columns:
                try:
                    frame[(MARKET_TICKERS[0], f)] = data[f].astype(float)
                except TypeError:
                    pass

    for yield_ticker in ["^TNX", "^TYX", "^IRX"]:
        for f in ["Open", "High", "Low", "Close"]:
            frame[(yield_ticker, f)] = _normalize_yield_series(frame[(yield_ticker, f)])

    gsr_close = (frame[("GC=F", "Close")] / frame[("SI=F", "Close")]).replace(
        [np.inf, -np.inf], np.nan
    )
    frame[("GSR", "Close")] = gsr_close
    for f in ["Open", "High", "Low", "Volume"]:
        frame[("GSR", f)] = np.nan

    return frame


def _fetch_yearly_history(period: str = "1y") -> Dict[str, Any]:
    """
    Returns daily bars for gold/silver futures, the GSR, and Treasury yields
    over the requested lookback. Serves the dashboard '1Y history' charts.
    Also snapshots the raw OHLCV frame for opportunistic archiving.
    """
    global _last_daily_frame
    frame = _download_daily_ohlcv(period=period)
    _last_daily_frame = frame

    rows = []
    for idx, row in frame.iterrows():
        def _val(ticker: str, decimals: int) -> Optional[float]:
            v = row[(ticker, "Close")]
            if pd.isna(v) or np.isinf(v):
                return None
            return round(float(v), decimals)

        rows.append(
            {
                "date": idx.isoformat(),
                "gold": _val("GC=F", 2),
                "silver": _val("SI=F", 2),
                "gsr": _val("GSR", 2),
                "us10y": _val("^TNX", 3),
                "us30y": _val("^TYX", 3),
                "us3m": _val("^IRX", 3),
            }
        )
    return {"period": period, "count": len(rows), "rows": rows}


class YearlyHistoryManager:
    """Thread-safe 1-hour TTL cache for the yearly daily-bar history endpoint."""

    def __init__(self, ttl_seconds: int = YEARLY_HISTORY_TTL_S):
        self.ttl = ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_history(self, period: str) -> Dict[str, Any]:
        async with self._lock:
            now = time.time()

            cached = self._cache.get(period)
            if cached and (now - cached["fetched_at"] < self.ttl):
                return cached["payload"]

            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(None, _fetch_yearly_history, period)
            self._cache[period] = {"payload": payload, "fetched_at": now}

            # Opportunistic archive: whenever a fresh daily fetch happens, mirror
            # the OHLCV bars into Supabase so a persistent archive accumulates.
            _launch_archive(_last_daily_frame)
            return payload


yearly_history_manager = YearlyHistoryManager()

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


def fetch_history(limit: int = 200, trading_date: Optional[str] = None) -> Dict[str, Any]:
    """Reads recent signal snapshots from Supabase, newest first, optionally
    filtered to a single GMT+8 trading day (YYYY-MM-DD)."""
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase persistence is not configured.")
    query = client.table(SUPABASE_TABLE).select("*").order("data_as_of", desc=True)
    if trading_date:
        query = query.eq("trading_date_gmt8", trading_date)
    resp = query.limit(limit).execute()
    return {"count": len(resp.data), "rows": resp.data}


def fetch_daily_summary(limit: int = 30) -> Dict[str, Any]:
    """
    Aggregates signal snapshots by GMT+8 trading day: snapshot count, OK/STALE
    mix, and signal-tag / arb-flag distributions (newest day first).
    """
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase persistence is not configured.")
    resp = client.table(SUPABASE_TABLE).select(
        "trading_date_gmt8, quality, signal_tag, arb_flag"
    ).limit(HISTORY_SCAN_LIMIT).execute()

    from collections import defaultdict
    daily = defaultdict(lambda: {"count": 0, "ok": 0, "stale": 0,
                                 "signals": defaultdict(int), "flags": defaultdict(int)})
    for r in resp.data:
        d = r.get("trading_date_gmt8")
        if not d:
            continue
        day = daily[d]
        day["count"] += 1
        if r.get("quality") == "OK":
            day["ok"] += 1
        else:
            day["stale"] += 1
        day["signals"][r.get("signal_tag", "NEUTRAL")] += 1
        day["flags"][r.get("arb_flag", "NONE")] += 1

    summary = []
    for d, agg in sorted(daily.items(), key=lambda kv: kv[0], reverse=True):
        summary.append({
            "trading_date_gmt8": d,
            "count": agg["count"],
            "ok": agg["ok"],
            "stale": agg["stale"],
            "signal_tags": dict(sorted(agg["signals"].items(), key=lambda kv: -kv[1])),
            "arb_flags": dict(sorted(agg["flags"].items(), key=lambda kv: -kv[1])),
        })
    return {"days": summary[:limit]}


# -----------------------------------------------------------------------------
# Daily Price History Archive (persistent OHLCV mirror in Supabase)
# -----------------------------------------------------------------------------

ARCHIVE_SYMBOLS = MARKET_TICKERS + ["GSR"]


def _num(v: Any, decimals: int = 4) -> Optional[float]:
    """Coerces a scalar to a rounded float, returning None for missing values."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f) or np.isinf(f):
        return None
    return round(f, decimals)


def _frame_to_archive_rows(frame: pd.DataFrame) -> list:
    """Flattens the (ticker, field) OHLCV frame into per-symbol daily rows."""
    rows = []
    for t in ARCHIVE_SYMBOLS:
        for idx, r in frame.iterrows():
            close = r[(t, "Close")]
            if pd.isna(close):
                continue
            rows.append({
                "symbol": t,
                "trading_date": idx.date().isoformat(),
                "open": _num(r[(t, "Open")]),
                "high": _num(r[(t, "High")]),
                "low": _num(r[(t, "Low")]),
                "close": _num(close),
                "volume": _num(r[(t, "Volume")], 0),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
    return rows


def _archive_daily_rows(rows: list) -> Dict[str, Any]:
    """Upserts daily price rows into Supabase keyed on (symbol, trading_date)."""
    if not rows:
        return {"rows_written": 0}
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase persistence is not configured.")
    written = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        client.table(PRICE_HISTORY_TABLE).upsert(
            chunk, on_conflict="symbol,trading_date"
        ).execute()
        written += len(chunk)
    return {"rows_written": written}


def _backfill_daily_archive(days: int) -> Dict[str, Any]:
    """Downloads ``days`` of daily OHLCV and persists them to Supabase."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    frame = _download_daily_ohlcv(start=start, end=end)
    rows = _frame_to_archive_rows(frame)
    result = _archive_daily_rows(rows)
    result["days"] = days
    result["symbols"] = list(ARCHIVE_SYMBOLS)
    result["latest_date"] = frame.index.max().date().isoformat()
    return result


def _launch_archive(frame: Optional[pd.DataFrame]) -> None:
    """Fire-and-forget background mirror of a freshly fetched OHLCV frame."""
    if frame is None or frame.empty or not PERSISTENCE_ENABLED:
        return
    rows = _frame_to_archive_rows(frame)
    if not rows:
        return

    def _job():
        try:
            res = _archive_daily_rows(rows)
            logger.info(f"Supabase: archived {res['rows_written']} daily price rows.")
        except Exception as e:
            logger.error(f"Supabase price archive failed (non-fatal): {str(e)}")

    threading.Thread(target=_job, daemon=True).start()


def fetch_price_history(
    symbol: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 1000,
) -> Dict[str, Any]:
    """Reads archived daily price rows from Supabase, newest date first."""
    client = _get_supabase()
    if client is None:
        raise RuntimeError("Supabase persistence is not configured.")
    query = client.table(PRICE_HISTORY_TABLE).select("*", count="exact").order("trading_date", desc=True)
    if symbol:
        query = query.eq("symbol", symbol)
    if start:
        query = query.gte("trading_date", start)
    if end:
        query = query.lte("trading_date", end)
    resp = query.limit(limit).execute()
    total = resp.count if getattr(resp, "count", None) is not None else len(resp.data)
    return {"count": len(resp.data), "total": total, "rows": resp.data}


def generate_llm_insights(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Best-effort executive synthesis via DeepSeek chat completions.
    Raises cleanly when the key is missing or the upstream call fails.
    """
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured.")
    prompt = (
        "You are an institutional macro desk analyst. Produce a concise, "
        "objective executive commentary (max ~120 words) on the current "
        "gold/silver/real-rates signal state. No disclaimers, no hype.\n\n"
        f"Signal state payload:\n{json.dumps(payload, indent=2, default=str)}"
    )
    response = requests.post(
        DEEPSEEK_BASE_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500,
        },
        timeout=DEEPSEEK_TIMEOUT_S,
    )
    response.raise_for_status()
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected DeepSeek response shape: {str(e)}")
    return {"model": DEEPSEEK_MODEL, "insight": content.strip()}


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


@app.post("/api/v1/insights")
async def get_insights_endpoint(payload: Dict[str, Any]):
    """
    Returns executive LLM synthesis for a signal state payload via DeepSeek.
    Feeds the dashboard 'AI Commentary' tab.
    """
    if not DEEPSEEK_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="DeepSeek API key is not configured (DEEPSEEK_API_KEY)."
        )
    try:
        return generate_llm_insights(payload)
    except Exception as e:
        logger.error(f"Insights generation failed: {str(e)}")
        raise HTTPException(status_code=502, detail=f"Insights generation error: {str(e)}")


@app.get("/api/v1/history")
async def get_history_endpoint(
    limit: int = Query(200, ge=1, le=5000),
    trading_date: Optional[str] = Query(None, description="Filter to one GMT+8 trading day (YYYY-MM-DD)")
):
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
        return fetch_history(limit=limit, trading_date=trading_date)
    except Exception as e:
        logger.error(f"History query failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"History query error: {str(e)}")


@app.get("/api/v1/history/daily")
async def get_daily_summary_endpoint(limit: int = Query(30, ge=1, le=365)):
    """
    Returns per-GMT+8-trading-day aggregates of persisted signal snapshots,
    newest day first. Feeds the dashboard daily summary table and date picker.
    """
    if not PERSISTENCE_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase persistence is not configured (SUPABASE_URL/SUPABASE_KEY missing)."
        )
    try:
        return fetch_daily_summary(limit=limit)
    except Exception as e:
        logger.error(f"Daily summary query failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Daily summary query error: {str(e)}")


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


@app.get("/api/v1/live")
async def get_live_quotes_endpoint(breakeven_10y: float = Query(2.28)):
    """
    Returns the latest gold/silver FUTURES prices and US Treasury yields with
    per-feed freshness, independent of the STALE signal gate. Protected by a
    10-second server-side TTL cache. Feeds the dashboard 'Live prices' tab.
    """
    try:
        return await live_quotes_manager.get_quotes(breakeven_10y=breakeven_10y)
    except Exception as e:
        logger.error(f"Live quotes generation failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Live quotes error: {str(e)}")


@app.get("/api/v1/live/yearly")
async def get_yearly_history_endpoint(period: str = Query("1y")):
    """
    Returns daily bars for gold/silver futures, GSR, and Treasury yields over
    the lookback period (1y, 6mo, 3mo, 1mo). Protected by a 1-hour server-side
    TTL cache. Feeds the dashboard '1Y history' charts.
    """
    if period not in ALLOWED_HISTORY_PERIODS:
        raise HTTPException(
            status_code=400,
            detail="period must be one of: 1y, 6mo, 3mo, 1mo"
        )
    try:
        return await yearly_history_manager.get_history(period=period)
    except Exception as e:
        logger.error(f"Yearly history generation failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Yearly history error: {str(e)}")


@app.post("/api/v1/price-history/backfill")
async def backfill_price_history_endpoint(days: int = Query(400, ge=30, le=2500)):
    """
    Downloads ``days`` of daily OHLCV (gold/silver futures, CBOE yields, GSR)
    and persists them into the Supabase price archive (idempotent upsert).
    """
    if not PERSISTENCE_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase persistence is not configured (SUPABASE_URL/SUPABASE_KEY missing)."
        )
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _backfill_daily_archive, days)
    except Exception as e:
        logger.error(f"Price archive backfill failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Price archive backfill error: {str(e)}")


@app.get("/api/v1/price-history")
async def get_price_history_endpoint(
    symbol: Optional[str] = Query(None, description="One of GC=F, SI=F, ^TNX, ^TYX, ^IRX, GSR"),
    start: Optional[str] = Query(None, description="Inclusive start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="Inclusive end date (YYYY-MM-DD)"),
    limit: int = Query(1000, ge=1, le=5000),
):
    """
    Returns archived daily price rows from the Supabase price_history_daily
    table, newest date first. Powers the dashboard archive viewer/download.
    """
    if not PERSISTENCE_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase persistence is not configured (SUPABASE_URL/SUPABASE_KEY missing)."
        )
    try:
        return fetch_price_history(symbol=symbol, start=start, end=end, limit=limit)
    except Exception as e:
        logger.error(f"Price history query failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Price history query error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)