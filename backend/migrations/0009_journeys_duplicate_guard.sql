-- Duplicate-add protection the application cannot provide alone.
--
-- 0005's journeys_ticket_leg_key is scoped to the TICKET (one row per ticket
-- per travelled leg), and every manual add mints a fresh ticket — so nothing
-- in the database stopped the same trip arriving twice via two requests. The
-- service's pre-insert existence check is a check-then-insert race: two
-- concurrent submissions (a double-tap, a client retry) both see "not there
-- yet" and both insert — and downstream that means the same delay detected
-- twice and two Delay Repay claims filed for one journey.
--
-- scheduled_departure is part of the key deliberately: the 08:14 and the
-- 18:14 MAN->EUS on the same date are two genuine journeys (0005's design
-- allows the same leg on a second ticket), while the same departure submitted
-- twice can only be a double-add.
--
-- If a "re-add after we gave up matching" flow lands later, replace this in a
-- NEW migration with a partial index excluding status = 'unmatched' — this
-- file is immutable once applied.

CREATE UNIQUE INDEX journeys_user_leg_departure_key
    ON journeys (user_id, travel_date, origin_crs, destination_crs, scheduled_departure);
