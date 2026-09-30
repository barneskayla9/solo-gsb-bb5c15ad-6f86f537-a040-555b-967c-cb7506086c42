-- Schema for the recycling-point service calendar.
-- Executed once at application startup (idempotent).

CREATE TABLE IF NOT EXISTS points (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL,                 -- IANA tz database name
    version     INTEGER NOT NULL DEFAULT 0,    -- number of published versions
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per *published* rule set.  Version 0 means "no rules yet" and is
-- never present; the point's current version always points at the newest row.
CREATE TABLE IF NOT EXISTS rule_versions (
    id          BIGSERIAL PRIMARY KEY,
    point_id    BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    payload     JSONB NOT NULL,                -- {weekly, closed_dates, makeup}
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (point_id, version)
);

CREATE INDEX IF NOT EXISTS idx_rule_versions_point
    ON rule_versions (point_id, version DESC);
