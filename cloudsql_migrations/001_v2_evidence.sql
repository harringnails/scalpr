CREATE SCHEMA IF NOT EXISTS scalpr_v2;
CREATE TABLE IF NOT EXISTS scalpr_v2.schema_migrations (
    version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS scalpr_v2.raw_capture (
    content_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL,
    symbol TEXT, schema_version TEXT NOT NULL,
    provider_timestamp TIMESTAMPTZ, received_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_capture_dataset_time
    ON scalpr_v2.raw_capture(provider, dataset, received_at);
CREATE INDEX IF NOT EXISTS idx_raw_capture_symbol_time
    ON scalpr_v2.raw_capture(symbol, received_at);
CREATE TABLE IF NOT EXISTS scalpr_v2.feature_snapshots (
    content_hash TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
    symbol TEXT NOT NULL, feature_version TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    missing_inputs JSONB NOT NULL DEFAULT '[]'::jsonb,
    features JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feature_snapshots_symbol_time
    ON scalpr_v2.feature_snapshots(symbol, observed_at);
CREATE TABLE IF NOT EXISTS scalpr_v2.trade_journal_mirror (
    row_hash TEXT PRIMARY KEY, source_row BIGINT NOT NULL,
    utc_time TIMESTAMPTZ, mode TEXT, symbol TEXT, raw_record JSONB NOT NULL,
    mirrored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trade_journal_mirror_time
    ON scalpr_v2.trade_journal_mirror(utc_time);
