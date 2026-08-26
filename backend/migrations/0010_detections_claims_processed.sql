-- The claims module's work queue, expressed on the detection row.
--
-- ARCHITECTURE §3 forbids cross-module table access, so the obvious query for
-- "entitled detections with no claim yet" — delay_detections LEFT JOIN claims
-- — cannot be written: it spans the delays and claims modules, and whichever
-- module held it would be reading the other's table.
--
-- 0006 already solved the same problem for notifications with notified_at:
-- a nullable timestamp ON THE DETECTION saying a downstream consumer is done
-- with it. This is that pattern a second time. The work query becomes
-- single-table and delays-owned, and claims drives it through
-- delays.service — the same shape as the delay sweep driving journey status
-- transitions through journeys.service.
--
-- Semantics, precisely: claims_processed_at means "the claims module has
-- FINISHED DECIDING about this detection", not "a claim exists". It is also
-- set when a claim provably cannot be created — a journey with no operator
-- has nothing to file against (claims.operator_id is NOT NULL) and would
-- otherwise occupy the sweep forever, the same poison-row failure the delay
-- sweep already had to fix. The absence of a claims row is the record of
-- which of the two happened.
--
-- Entitlement 0 detections ('late but under threshold' — 0006) never become
-- claims, so they are excluded from the index rather than stamped: nothing
-- should ever process them, and leaving them NULL keeps that visible.

ALTER TABLE delay_detections
    ADD COLUMN claims_processed_at timestamptz;

-- Mirror of delay_detections_pending_idx for the claims consumer.
CREATE INDEX delay_detections_unclaimed_idx ON delay_detections (observed_at, id)
    WHERE claims_processed_at IS NULL AND entitlement_pence > 0;

COMMENT ON COLUMN delay_detections.claims_processed_at IS
    'When the claims module finished deciding about this detection: a claim '
    'was created, or one provably cannot be (no operator on the journey). '
    'NULL and entitlement_pence > 0 = still in the claims work queue.';
