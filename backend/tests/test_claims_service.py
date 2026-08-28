"""Claims tests: the state machine and its audit trail, the creation sweep over
entitled detections, and the filing-deadline sweep.

Everything runs against real Postgres via the rollback `conn` fixture — the
state machine's guards are half SQL (the guarded UPDATE, the CHECK constraints)
and a mocked database would prove none of them.

Tests build their own operator ('QQ' — not a real TOC, so it can never collide
with seeded reference data) so every deadline and pence figure here is
checkable by hand.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

# Imported directly, unusually: the lost-race test drives the guarded UPDATE
# itself, because reproducing the race through the service would need threads.
from autotrain.modules.claims import repository as _repository
from autotrain.modules.claims.service import (
    ClaimContext,
    FilingUnavailable,
    IllegalTransition,
    NotClaimable,
    NotFilable,
    UnclaimedDetection,
    UnknownClaim,
    claim_history,
    expire_overdue,
    file_claim,
    get_claim,
    list_claims,
    open_claim,
    run_claim_sweep,
    transition,
)
from conftest import mk_user as _mk_user
from conftest import scalar as _scalar

NOW = datetime.now(tz=UTC)
ARRIVAL = NOW - timedelta(hours=30)
DEPARTURE = ARRIVAL - timedelta(hours=2)
TRAVEL_DATE = DEPARTURE.date()
CLAIM_WINDOW_DAYS = 28

# 50% of a £45.50 single: the figure every expectation below is built from.
ENTITLEMENT = 2275

# Real three-letter CRS codes: origin_crs is CHECK (~ '^[A-Z]{3}$'), so a
# generated "S00" would be rejected by the database, not by the test.
DISTINCT_ORIGINS = ("MAN", "LDS", "YRK", "BHM", "LIV", "SHF", "NCL")

# Not a real portal: filing tests only ever check the URL passes through.
CLAIM_URL = "https://delayrepay.test-railways.example/claim"


# --- Row builders ------------------------------------------------------------


def _mk_operator(
    conn: psycopg.Connection,
    atoc: str = "QQ",
    claim_window_days: int = CLAIM_WINDOW_DAYS,
    *,
    adapter: str = "none",
    claim_url: str | None = None,
    is_active: bool = True,
) -> UUID:
    return _scalar(
        conn.execute(
            "INSERT INTO operators (atoc_code, name, min_delay_minutes, claim_window_days, "
            "adapter, claim_url, is_active) "
            "VALUES (%s, 'Test Railways', 15, %s, %s, %s, %s) RETURNING id",
            (atoc, claim_window_days, adapter, claim_url, is_active),
        )
    )


def _mk_journey(
    conn: psycopg.Connection,
    user_id: UUID,
    *,
    operator_id: UUID | None,
    departure: datetime = DEPARTURE,
    origin: str = "MAN",
    destination: str = "EUS",
    price_pence: int = 4550,
) -> UUID:
    ticket_id = _scalar(
        conn.execute(
            "INSERT INTO tickets (user_id, kind, price_pence, source) "
            "VALUES (%s, 'single', %s, 'manual') RETURNING id",
            (user_id, price_pence),
        )
    )
    return _scalar(
        conn.execute(
            "INSERT INTO journeys (user_id, ticket_id, operator_id, origin_crs, "
            "destination_crs, travel_date, scheduled_departure, scheduled_arrival, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'assessed') RETURNING id",
            (
                user_id,
                ticket_id,
                operator_id,
                origin,
                destination,
                departure.date(),
                departure,
                departure + timedelta(hours=2),
            ),
        )
    )


def _mk_detection(
    conn: psycopg.Connection, journey_id: UUID, *, entitlement_pence: int = ENTITLEMENT
) -> UUID:
    return _scalar(
        conn.execute(
            "INSERT INTO delay_detections "
            "(journey_id, actual_arrival, delay_minutes, source, band_percent, entitlement_pence) "
            "VALUES (%s, %s, 45, 'hsp', 50, %s) RETURNING id",
            (journey_id, ARRIVAL + timedelta(minutes=45), entitlement_pence),
        )
    )


def _entitled_journey(
    conn: psycopg.Connection,
    *,
    operator_id: UUID | None,
    user_id: UUID,
    departure: datetime = DEPARTURE,
    origin: str = "MAN",
    entitlement_pence: int = ENTITLEMENT,
) -> tuple[UUID, UUID]:
    """(journey_id, detection_id) for a journey with a frozen delay decision."""
    journey_id = _mk_journey(
        conn, user_id, operator_id=operator_id, departure=departure, origin=origin
    )
    return journey_id, _mk_detection(conn, journey_id, entitlement_pence=entitlement_pence)


def _claim(conn: psycopg.Connection, journey_id: UUID) -> tuple | None:
    return conn.execute(
        "SELECT id, status, amount_pence, file_by, submitted_at, resolved_at "
        "FROM claims WHERE journey_id = %s",
        (journey_id,),
    ).fetchone()


def _claim_count(conn: psycopg.Connection, journey_id: UUID) -> int:
    return _scalar(conn.execute("SELECT count(*) FROM claims WHERE journey_id = %s", (journey_id,)))


def _processed_at(conn: psycopg.Connection, detection_id: UUID) -> datetime | None:
    return _scalar(
        conn.execute(
            "SELECT claims_processed_at FROM delay_detections WHERE id = %s", (detection_id,)
        )
    )


def _context(conn: psycopg.Connection, journey_id: UUID) -> ClaimContext:
    row = conn.execute(
        "SELECT j.user_id, j.operator_id, j.travel_date, o.claim_window_days "
        "FROM journeys j LEFT JOIN operators o ON o.id = j.operator_id WHERE j.id = %s",
        (journey_id,),
    ).fetchone()
    assert row is not None
    return ClaimContext(
        journey_id=journey_id,
        user_id=row[0],
        operator_id=row[1],
        travel_date=row[2],
        claim_window_days=row[3],
    )


def _detection_row(conn: psycopg.Connection, detection_id: UUID) -> UnclaimedDetection:
    row = conn.execute(
        "SELECT journey_id, entitlement_pence, observed_at FROM delay_detections WHERE id = %s",
        (detection_id,),
    ).fetchone()
    assert row is not None
    return UnclaimedDetection(
        id=detection_id, journey_id=row[0], entitlement_pence=row[1], observed_at=row[2]
    )


# --- open_claim --------------------------------------------------------------


def test_open_claim_freezes_amount_and_deadline(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    operator_id = _mk_operator(conn)
    journey_id, detection_id = _entitled_journey(conn, operator_id=operator_id, user_id=user_id)

    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))

    assert claim is not None
    assert claim.status == "draft"
    # The detection's entitlement, copied — never recomputed.
    assert claim.amount_pence == ENTITLEMENT
    assert claim.file_by == TRAVEL_DATE + timedelta(days=CLAIM_WINDOW_DAYS)
    assert claim.submitted_at is None and claim.resolved_at is None


def test_open_claim_writes_the_creation_event(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id
    )

    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None

    history = claim_history(conn, claim.id)
    assert len(history) == 1
    # NULL from_status is the creation event (0006).
    assert history[0].from_status is None
    assert history[0].to_status == "draft"


def test_open_claim_is_idempotent(conn: psycopg.Connection) -> None:
    """Guarantee 2 of 0006: at most one claim per journey. The second attempt
    is absorbed rather than raising, because two concurrent sweeps reaching the
    same detection is normal, not exceptional."""
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id
    )
    detection = _detection_row(conn, detection_id)
    context = _context(conn, journey_id)

    first = open_claim(conn, detection, context)
    second = open_claim(conn, detection, context)

    assert first is not None
    assert second is None
    assert _claim_count(conn, journey_id) == 1
    # ...and the loser wrote no phantom creation event.
    assert len(claim_history(conn, first.id)) == 1


def test_open_claim_refuses_a_journey_with_no_operator(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(conn, operator_id=None, user_id=user_id)

    with pytest.raises(NotClaimable):
        open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))


def test_open_claim_still_creates_an_already_late_back_claim(conn: psycopg.Connection) -> None:
    """A journey older than the filing window still gets a claim: the deadline
    sweep expires it with an audit trail, rather than it vanishing silently."""
    user_id = _mk_user(conn)
    old_departure = DEPARTURE - timedelta(days=90)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id, departure=old_departure
    )

    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))

    assert claim is not None
    assert claim.file_by < date.today()


# --- The state machine -------------------------------------------------------


def _draft_claim(conn: psycopg.Connection) -> UUID:
    """A freshly created claim in 'draft' — the starting point for every state
    machine test below."""
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=_mk_user(conn)
    )
    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None
    return claim.id


def test_transition_moves_and_audits(conn: psycopg.Connection) -> None:
    claim_id = _draft_claim(conn)

    assert transition(conn, claim_id, "ready", detail="auto-file enabled") is True

    history = claim_history(conn, claim_id)
    assert [(e.from_status, e.to_status) for e in history] == [
        (None, "draft"),
        ("draft", "ready"),
    ]
    assert history[-1].detail == "auto-file enabled"


def test_transition_rejects_an_edge_the_machine_does_not_have(conn: psycopg.Connection) -> None:
    claim_id = _draft_claim(conn)

    # draft -> paid skips filing entirely: a caller bug, so it raises rather
    # than returning False (which means "lost a race").
    with pytest.raises(IllegalTransition):
        transition(conn, claim_id, "paid")

    # Nothing moved, and nothing was written to the audit trail.
    assert len(claim_history(conn, claim_id)) == 1


def test_terminal_states_have_no_exits(conn: psycopg.Connection) -> None:
    claim_id = _draft_claim(conn)
    assert transition(conn, claim_id, "ready")
    assert transition(conn, claim_id, "submitted")
    assert transition(conn, claim_id, "rejected")

    for attempt in ("ready", "submitted", "approved", "paid", "expired"):
        with pytest.raises(IllegalTransition):
            transition(conn, claim_id, attempt)


def test_transition_stamps_submitted_at_and_resolved_at(conn: psycopg.Connection) -> None:
    claim_id = _draft_claim(conn)
    assert transition(conn, claim_id, "ready")
    assert transition(conn, claim_id, "submitted")

    row = conn.execute(
        "SELECT submitted_at, resolved_at FROM claims WHERE id = %s", (claim_id,)
    ).fetchone()
    assert row is not None
    submitted_at, resolved_at = row
    assert submitted_at is not None
    # 'approved' and 'submitted' are not resolutions: the money has not moved.
    assert resolved_at is None

    assert transition(conn, claim_id, "approved")
    resolved = _scalar(conn.execute("SELECT resolved_at FROM claims WHERE id = %s", (claim_id,)))
    assert resolved is None

    assert transition(conn, claim_id, "paid")
    row = conn.execute(
        "SELECT submitted_at, resolved_at FROM claims WHERE id = %s", (claim_id,)
    ).fetchone()
    assert row is not None
    # submitted_at is not clobbered by later moves.
    assert row[0] == submitted_at
    assert row[1] is not None


def test_transition_on_an_unknown_claim(conn: psycopg.Connection) -> None:
    with pytest.raises(UnknownClaim):
        transition(conn, uuid4(), "ready")


def test_transition_reports_a_lost_race_as_false(conn: psycopg.Connection) -> None:
    """The guarded UPDATE is what makes the read-then-write safe: if the status
    moves between them, this call must decline rather than overwrite."""
    claim_id = _draft_claim(conn)

    # Stand in for the racing writer: move the row out from under the guard,
    # then replay the UPDATE the service would have issued with the status it
    # had read. The repository takes from_status explicitly, so the race is
    # reproducible without threads.
    conn.execute("UPDATE claims SET status = 'needs_user' WHERE id = %s", (claim_id,))
    assert (
        _repository.transition(conn, claim_id=claim_id, from_status="draft", to_status="ready")
        is False
    )
    assert _scalar(conn.execute("SELECT status FROM claims WHERE id = %s", (claim_id,))) == (
        "needs_user"
    )


# --- The creation sweep ------------------------------------------------------


def test_sweep_opens_claims_and_stamps_the_detections(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    operator_id = _mk_operator(conn)
    first, first_detection = _entitled_journey(
        conn, operator_id=operator_id, user_id=user_id, origin="MAN"
    )
    second, second_detection = _entitled_journey(
        conn,
        operator_id=operator_id,
        user_id=user_id,
        origin="LDS",
        departure=DEPARTURE + timedelta(hours=3),
    )

    stats = run_claim_sweep(conn)

    assert (stats.examined, stats.opened, stats.errors) == (2, 2, 0)
    for journey_id in (first, second):
        row = _claim(conn, journey_id)
        assert row is not None and row[1] == "draft" and row[2] == ENTITLEMENT
    # Stamped, so they never come back round.
    assert _processed_at(conn, first_detection) is not None
    assert _processed_at(conn, second_detection) is not None


def test_sweep_ignores_detections_under_the_threshold(conn: psycopg.Connection) -> None:
    """entitlement_pence = 0 is 'late but under threshold' (0006): recorded,
    never claimed — and never stamped either, since nothing should process it."""
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id, entitlement_pence=0
    )

    stats = run_claim_sweep(conn)

    assert stats.examined == 0
    assert _claim(conn, journey_id) is None
    assert _processed_at(conn, detection_id) is None


def test_sweep_retires_a_detection_with_no_operator(conn: psycopg.Connection) -> None:
    """Nothing to file against, and that cannot change — so it is stamped and
    leaves the queue instead of being re-examined by every future sweep."""
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(conn, operator_id=None, user_id=user_id)

    stats = run_claim_sweep(conn)

    assert (stats.examined, stats.opened, stats.no_operator) == (1, 0, 1)
    assert _claim(conn, journey_id) is None
    assert _processed_at(conn, detection_id) is not None
    # ...and a second sweep finds no work at all.
    assert run_claim_sweep(conn).examined == 0


def test_sweep_is_idempotent(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    journey_id, _ = _entitled_journey(conn, operator_id=_mk_operator(conn), user_id=user_id)

    first = run_claim_sweep(conn)
    second = run_claim_sweep(conn)

    assert (first.examined, first.opened) == (1, 1)
    assert (second.examined, second.opened) == (0, 0)
    assert _claim_count(conn, journey_id) == 1


def test_sweep_pages_through_more_than_one_batch(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    operator_id = _mk_operator(conn)
    for index in range(5):
        _entitled_journey(
            conn,
            operator_id=operator_id,
            user_id=user_id,
            departure=DEPARTURE + timedelta(hours=index),
            origin=DISTINCT_ORIGINS[index],
        )

    stats = run_claim_sweep(conn, batch_size=2)

    assert (stats.examined, stats.opened) == (5, 5)
    assert _scalar(conn.execute("SELECT count(*) FROM claims")) == 5


def test_sweep_counts_a_claim_another_writer_already_opened(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id
    )
    # A competing sweep got the claim in but had not stamped the detection yet.
    open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))

    stats = run_claim_sweep(conn)

    assert (stats.examined, stats.opened, stats.already_claimed) == (1, 0, 1)
    assert _processed_at(conn, detection_id) is not None


# --- The deadline sweep ------------------------------------------------------


def test_expire_overdue_expires_unfiled_claims_with_an_audit_trail(
    conn: psycopg.Connection,
) -> None:
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id
    )
    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None

    expired = expire_overdue(conn, claim.file_by + timedelta(days=1))

    assert expired == 1
    row = _claim(conn, journey_id)
    assert row is not None and row[1] == "expired"
    assert row[5] is not None  # resolved_at
    assert [e.to_status for e in claim_history(conn, claim.id)] == ["draft", "expired"]


def test_expire_overdue_leaves_claims_inside_the_window(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id
    )
    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None

    # file_by is the last day that still counts, so the boundary is not overdue.
    assert expire_overdue(conn, claim.file_by) == 0
    row = _claim(conn, journey_id)
    assert row is not None and row[1] == "draft"


def test_expire_overdue_leaves_needs_user_alone(conn: psycopg.Connection) -> None:
    """The user holds a deep link and may have filed it themselves — we do not
    know the claim is dead, so we will not say it is."""
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id
    )
    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None
    assert transition(conn, claim.id, "needs_user")

    assert expire_overdue(conn, claim.file_by + timedelta(days=365)) == 0
    row = _claim(conn, journey_id)
    assert row is not None and row[1] == "needs_user"


def test_expire_overdue_leaves_filed_claims_alone(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=user_id
    )
    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None
    assert transition(conn, claim.id, "ready")
    assert transition(conn, claim.id, "submitted")

    assert expire_overdue(conn, claim.file_by + timedelta(days=365)) == 0
    row = _claim(conn, journey_id)
    assert row is not None and row[1] == "submitted"


# --- Filing (v1: the deep-link handoff) ---------------------------------------


def _filable_claim(conn: psycopg.Connection, user_id: UUID, **operator_kw) -> UUID:
    """A draft claim whose operator has the deep-link adapter enabled (unless
    the test overrides that via operator kwargs)."""
    operator_kw.setdefault("adapter", "deep_link")
    operator_kw.setdefault("claim_url", CLAIM_URL)
    operator_id = _mk_operator(conn, **operator_kw)
    journey_id, detection_id = _entitled_journey(conn, operator_id=operator_id, user_id=user_id)
    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None
    return claim.id


def _status(conn: psycopg.Connection, claim_id: UUID) -> str:
    return _scalar(conn.execute("SELECT status FROM claims WHERE id = %s", (claim_id,)))


def test_file_claim_hands_over_the_link_and_parks_the_claim(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)

    filing = file_claim(conn, claim_id, user_id)

    assert filing is not None
    assert filing.url == CLAIM_URL
    assert filing.status == "needs_user"
    assert _status(conn, claim_id) == "needs_user"
    # The move is audited like every other transition...
    history = claim_history(conn, claim_id)
    assert [(e.from_status, e.to_status) for e in history] == [
        (None, "draft"),
        ("draft", "needs_user"),
    ]
    assert history[-1].detail == "deep link issued to the user"
    # ...and from here the deadline sweep leaves the claim alone (the user may
    # have filed it) — the contract test_expire_overdue_leaves_needs_user_alone
    # proves from the sweep's side.


def test_file_claim_again_returns_the_same_link_and_writes_nothing(
    conn: psycopg.Connection,
) -> None:
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)

    first = file_claim(conn, claim_id, user_id)
    second = file_claim(conn, claim_id, user_id)

    # Losing your link is recoverable, and recovering it is not an event.
    assert first == second
    assert len(claim_history(conn, claim_id)) == 2


def test_file_claim_scopes_by_owner(conn: psycopg.Connection) -> None:
    owner = _mk_user(conn, "owner@example.com")
    stranger = _mk_user(conn, "stranger@example.com")
    claim_id = _filable_claim(conn, owner)

    # The stranger sees nothing AND moved nothing; the owner filing afterwards
    # proves the claim was filable all along (the control for the None above).
    assert file_claim(conn, claim_id, stranger) is None
    assert _status(conn, claim_id) == "draft"
    assert file_claim(conn, claim_id, owner) is not None


def test_file_claim_on_an_unknown_claim_is_none(conn: psycopg.Connection) -> None:
    assert file_claim(conn, uuid4(), _mk_user(conn)) is None


def test_file_claim_refuses_a_claim_past_filing(conn: psycopg.Connection) -> None:
    """ready is queued for v2 auto-submission and submitted is already with
    the operator — handing the user the form as well would file it twice."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)
    assert transition(conn, claim_id, "ready")

    with pytest.raises(NotFilable):
        file_claim(conn, claim_id, user_id)

    assert transition(conn, claim_id, "submitted")
    with pytest.raises(NotFilable):
        file_claim(conn, claim_id, user_id)
    # The refusals moved nothing and audited nothing.
    assert _status(conn, claim_id) == "submitted"
    assert len(claim_history(conn, claim_id)) == 3


def test_file_claim_refuses_a_resolved_claim(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)
    assert transition(conn, claim_id, "expired")

    with pytest.raises(NotFilable):
        file_claim(conn, claim_id, user_id)


def test_file_claim_refuses_an_overdue_back_claim(conn: psycopg.Connection) -> None:
    """A back-claim past its window is created on purpose (open_claim) and
    expired by the nightly sweep. Filing must not beat the sweep to it:
    needs_user is a state the sweep never expires, so handing out the link
    would leave the dead amount in pending_pence forever."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)
    deadline = TRAVEL_DATE + timedelta(days=CLAIM_WINDOW_DAYS)

    with pytest.raises(NotFilable):
        file_claim(conn, claim_id, user_id, today=deadline + timedelta(days=1))
    # Left in draft, so the deadline sweep still gives it an audited 'expired'.
    assert _status(conn, claim_id) == "draft"
    assert len(claim_history(conn, claim_id)) == 1


def test_file_claim_allows_the_deadline_day_itself(conn: psycopg.Connection) -> None:
    """file_by is the last day that still counts — the same boundary as the
    sweep (test_expire_overdue_leaves_claims_inside_the_window)."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)
    deadline = TRAVEL_DATE + timedelta(days=CLAIM_WINDOW_DAYS)

    filing = file_claim(conn, claim_id, user_id, today=deadline)

    assert filing is not None and filing.status == "needs_user"


def test_file_claim_without_an_enabled_adapter(conn: psycopg.Connection) -> None:
    """0008 seeds every operator at 'none' until its portal URL is verified —
    those claims exist and accrue, but there is no link to hand out yet."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id, adapter="none", claim_url=None)

    with pytest.raises(FilingUnavailable):
        file_claim(conn, claim_id, user_id)
    assert _status(conn, claim_id) == "draft"


def test_file_claim_refuses_an_inactive_operators_stale_link(conn: psycopg.Connection) -> None:
    """is_active gates filing, not pricing: a franchise change kills the
    portal, so the stored link must not be handed out — even to a claim that
    was filable before the change."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id, is_active=False)

    with pytest.raises(FilingUnavailable):
        file_claim(conn, claim_id, user_id)
    assert _status(conn, claim_id) == "draft"


def test_file_claim_stops_reissuing_after_the_operator_dies(conn: psycopg.Connection) -> None:
    """The idempotent needs_user path still goes through the adapter gate: a
    user who lost their link and asks again after a franchise change must get
    FilingUnavailable, never the dead portal's URL. This pins the ordering —
    the adapter check sits ABOVE the needs_user early-return."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)
    assert file_claim(conn, claim_id, user_id) is not None
    conn.execute("UPDATE operators SET is_active = false WHERE atoc_code = 'QQ'")

    with pytest.raises(FilingUnavailable):
        file_claim(conn, claim_id, user_id)


def test_file_claim_form_submit_is_unavailable_in_v1(conn: psycopg.Connection) -> None:
    """'form_submit' names the v2 server-side adapter, which does not exist
    yet: the registry must miss, never fall back to handing out the form —
    v2 would also submit it, the double filing NotFilable exists to stop."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id, adapter="form_submit")

    with pytest.raises(FilingUnavailable):
        file_claim(conn, claim_id, user_id)
    assert _status(conn, claim_id) == "draft"


def test_file_claim_survives_losing_the_double_tap_race(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two taps race: the loser's guarded UPDATE finds the winner already
    parked the claim in needs_user (rowcount 0), and must degrade to handing
    back the same link — never an error. The interleaving needs threads, so
    the winner is injected just before the loser's UPDATE runs; every
    statement is still real SQL against the real schema."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)
    real_transition = _repository.transition

    def _the_other_tap_wins_first(
        inner: psycopg.Connection, *, claim_id: UUID, from_status: str, to_status: str
    ) -> bool:
        assert real_transition(conn, claim_id=claim_id, from_status="draft", to_status="needs_user")
        return real_transition(
            inner, claim_id=claim_id, from_status=from_status, to_status=to_status
        )

    monkeypatch.setattr(_repository, "transition", _the_other_tap_wins_first)

    filing = file_claim(conn, claim_id, user_id)

    assert filing is not None
    assert filing.url == CLAIM_URL
    assert _status(conn, claim_id) == "needs_user"
    # The loser wrote no audit event: only the creation event exists (the
    # injected winner drives the repository directly, so its event is not
    # simulated). A loser that wrote 'deep link issued' despite rowcount 0
    # would corrupt the trail — this is the assert that catches it.
    assert len(claim_history(conn, claim_id)) == 1


def test_file_claim_losing_to_a_real_state_change_raises(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other race branch: the winner was not a second tap but the expiry
    sweep. The loser must refuse with the domain error the router turns into
    a 409 — anything else escaping here is a 500 for the user."""
    user_id = _mk_user(conn)
    claim_id = _filable_claim(conn, user_id)
    real_transition = _repository.transition

    def _the_sweep_wins_first(
        inner: psycopg.Connection, *, claim_id: UUID, from_status: str, to_status: str
    ) -> bool:
        assert real_transition(conn, claim_id=claim_id, from_status="draft", to_status="expired")
        return real_transition(
            inner, claim_id=claim_id, from_status=from_status, to_status=to_status
        )

    monkeypatch.setattr(_repository, "transition", _the_sweep_wins_first)

    with pytest.raises(NotFilable):
        file_claim(conn, claim_id, user_id)
    assert _status(conn, claim_id) == "expired"


# --- Read paths --------------------------------------------------------------


def test_get_claim_scopes_by_owner(conn: psycopg.Connection) -> None:
    owner = _mk_user(conn, "owner@example.com")
    stranger = _mk_user(conn, "stranger@example.com")
    journey_id, detection_id = _entitled_journey(
        conn, operator_id=_mk_operator(conn), user_id=owner
    )
    claim = open_claim(conn, _detection_row(conn, detection_id), _context(conn, journey_id))
    assert claim is not None

    assert get_claim(conn, claim.id, owner) is not None
    # Someone else's claim is indistinguishable from one that does not exist.
    assert get_claim(conn, claim.id, stranger) is None
    assert list_claims(conn, stranger) == []


def test_list_claims_is_newest_first(conn: psycopg.Connection) -> None:
    user_id = _mk_user(conn)
    operator_id = _mk_operator(conn)
    for index in range(3):
        _entitled_journey(
            conn,
            operator_id=operator_id,
            user_id=user_id,
            departure=DEPARTURE + timedelta(hours=index),
            origin=DISTINCT_ORIGINS[index],
        )
    run_claim_sweep(conn)

    claims = list_claims(conn, user_id)

    assert len(claims) == 3
    assert [c.created_at for c in claims] == sorted((c.created_at for c in claims), reverse=True)
