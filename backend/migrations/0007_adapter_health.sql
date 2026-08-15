-- Adapter health — the treadmill monitor.
--
-- PLAN.md §5: "operators change their forms, so build adapters with
-- monitoring/alerts for breakage from day one." Every server-side submission
-- attempt records an outcome here; the alerting query is "failures for operator
-- X in the last N attempts", answered by the partial index below.
--
-- Append-only. Volume is bounded by claim volume (a few rows per claim at
-- worst), so no partitioning or rotation is needed for a long time.

CREATE TABLE adapter_runs (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id  uuid        NOT NULL REFERENCES operators (id),
    claim_id     uuid        REFERENCES claims (id) ON DELETE SET NULL,
    outcome      text        NOT NULL CHECK (outcome IN ('success', 'failure', 'timeout')),
    -- Error class + message for failures; kept small, full traces go to logs.
    detail       text,
    duration_ms  integer     CHECK (duration_ms >= 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX adapter_runs_health_idx ON adapter_runs (operator_id, created_at DESC);
CREATE INDEX adapter_runs_failures_idx ON adapter_runs (operator_id, created_at DESC)
    WHERE outcome <> 'success';

COMMENT ON TABLE adapter_runs IS
    'One row per claim-submission attempt. The data behind "GWR adapter is '
    'failing since Tuesday" alerts — a broken form shows up here first.';
