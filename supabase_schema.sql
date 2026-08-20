-- =============================================================================
-- Supabase Schema v0.3.0 - Signal Snapshot Persistence
-- Run this in: Supabase Dashboard -> SQL Editor -> New query -> Run
-- =============================================================================

-- Core signal snapshots table (one row per unique 1-minute engine bar)
create table if not exists public.signal_snapshots (
    id              bigint generated always as identity primary key,
    data_as_of      timestamptz not null unique,   -- engine bar timestamp (upsert key)
    trading_date_gmt8 date
                    generated always as ((data_as_of at time zone 'Asia/Hong_Kong')::date) stored,
    signal_tag      text not null,
    arb_flag        text not null,
    quality         text not null,
    rr_z            double precision,
    gold_z          double precision,
    gsr_z           double precision,
    gold_price      double precision,
    silver_price    double precision,
    gsr_ratio       double precision,
    real_yield_10y  double precision,
    slope_10y3m     double precision,
    slope_30y10y    double precision,
    data_source_gold   text not null,
    data_source_silver text not null,
    breakeven_10y   double precision not null default 2.28,
    period          text not null default '1d',
    engine_version  text not null,
    created_at      timestamptz not null default now()
);

-- Indexes for time-series queries
create index if not exists idx_signal_snapshots_asof
    on public.signal_snapshots (data_as_of desc);
create index if not exists idx_signal_snapshots_trading_date
    on public.signal_snapshots (trading_date_gmt8 desc);
create index if not exists idx_signal_snapshots_quality
    on public.signal_snapshots (quality);
create index if not exists idx_signal_snapshots_engine_version
    on public.signal_snapshots (engine_version);

-- Row Level Security: default deny, then allow anon key
-- (server-side service-role key bypasses RLS entirely)
alter table public.signal_snapshots enable row level security;

drop policy if exists "anon_insert_signal_snapshots" on public.signal_snapshots;
create policy "anon_insert_signal_snapshots"
    on public.signal_snapshots for insert
    to anon with check (true);

drop policy if exists "anon_select_signal_snapshots" on public.signal_snapshots;
create policy "anon_select_signal_snapshots"
    on public.signal_snapshots for select
    to anon using (true);

-- =============================================================================
-- Price History Archive (daily OHLCV bars, one row per symbol per trading day)
-- =============================================================================

create table if not exists public.price_history_daily (
    id              bigint generated always as identity primary key,
    symbol          text not null,           -- GC=F, SI=F, ^TNX, ^TYX, ^IRX, GSR
    trading_date    date not null,
    open            double precision,
    high            double precision,
    low             double precision,
    close           double precision,
    volume          double precision,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (symbol, trading_date)
);

create index if not exists idx_price_history_symbol_date
    on public.price_history_daily (symbol, trading_date desc);
create index if not exists idx_price_history_date
    on public.price_history_daily (trading_date desc);

-- RLS: allow anon + authenticated full access (matches signal_snapshots posture)
alter table public.price_history_daily enable row level security;

drop policy if exists "anon_all_price_history_daily" on public.price_history_daily;
create policy "anon_all_price_history_daily"
    on public.price_history_daily for all
    to anon using (true) with check (true);

drop policy if exists "authenticated_all_price_history_daily" on public.price_history_daily;
create policy "authenticated_all_price_history_daily"
    on public.price_history_daily for all
    to authenticated using (true) with check (true);

-- =============================================================================
-- Chat History (DeepSeek Q&A log, one row per exchange)
-- =============================================================================

create table if not exists public.chat_log (
    id              bigint generated always as identity primary key,
    created_at      timestamptz not null default now(),
    session_id      text not null,
    user_message    text not null,
    assistant_reply text not null,
    quality         text,
    engine_version  text
);

create index if not exists idx_chat_log_session
    on public.chat_log (session_id, created_at desc);

alter table public.chat_log enable row level security;

drop policy if exists "anon_all_chat_log" on public.chat_log;
create policy "anon_all_chat_log"
    on public.chat_log for all
    to anon using (true) with check (true);

drop policy if exists "authenticated_all_chat_log" on public.chat_log;
create policy "authenticated_all_chat_log"
    on public.chat_log for all
    to authenticated using (true) with check (true);
