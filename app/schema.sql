-- Schema for the recycling-point service calendar and visit appointments.
-- Executed once at application startup (idempotent).

CREATE TABLE IF NOT EXISTS points (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL,                 -- IANA tz database name
    version     INTEGER NOT NULL DEFAULT 0,    -- number of published versions
    -- Bumped on every rule publication and timezone change.  Bookings
    -- validate against one epoch and re-check it under the point row lock,
    -- closing the "insert booking, then commit a stranding calendar change"
    -- race even with the change's rules hidden inside JSONB.
    calendar_epoch BIGINT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent migration for databases created before calendar_epoch existed.
ALTER TABLE points ADD COLUMN IF NOT EXISTS calendar_epoch BIGINT NOT NULL DEFAULT 0;

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

-- Visit appointments booked against the *currently published* calendar.
-- rules_version remembers which published rule set was open at booking time;
-- every still-future active appointment must stay entirely inside the open
-- intervals of the current version and timezone.
CREATE TABLE IF NOT EXISTS appointments (
    id              BIGSERIAL PRIMARY KEY,
    point_id        BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    start_at        TIMESTAMPTZ NOT NULL,
    end_at          TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT NOT NULL,
    cancel_token    TEXT NOT NULL,             -- shown to the client only
    status          TEXT NOT NULL DEFAULT 'booked',  -- booked | cancelled
    rules_version   INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ,
    UNIQUE (point_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_appointments_point_time
    ON appointments (point_id, start_at, end_at);

-- Active appointments at one point may never overlap in UTC; touching
-- (end == next start) is allowed; cancelled rows never participate.
--
-- Implemented with an extension-free trigger instead of a GiST EXCLUDE so the
-- suite (pgserver-bundled PostgreSQL, no contrib) and the Docker image behave
-- identically.  The guard first takes FOR UPDATE on the parent point row:
-- writers of one point serialize on that lock, so under READ COMMITTED the
-- overlap SELECT of a waiter always observes the rows committed by the
-- holder -- the check is therefore race-free for direct SQL writes too.
CREATE OR REPLACE FUNCTION appointments_no_overlap_guard()
RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'booked' THEN
        PERFORM id FROM points WHERE id = NEW.point_id FOR UPDATE;
        IF EXISTS (
            SELECT 1
            FROM appointments a
            WHERE a.point_id = NEW.point_id
              AND a.status = 'booked'
              AND a.id IS DISTINCT FROM NEW.id
              AND a.start_at < NEW.end_at
              AND NEW.start_at < a.end_at
        ) THEN
            RAISE EXCEPTION 'overlapping active appointment for point %',
                NEW.point_id
                USING ERRCODE = 'P0001', DETAIL = 'appointment_conflict';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_appointments_no_overlap ON appointments;
CREATE TRIGGER trg_appointments_no_overlap
    AFTER INSERT OR UPDATE OF point_id, start_at, end_at, status
    ON appointments
    FOR EACH ROW EXECUTE FUNCTION appointments_no_overlap_guard();
