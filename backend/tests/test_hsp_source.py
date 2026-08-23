"""HSP source tests. No network, no database: the journey dataclass is built
directly and HSP itself is played by httpx.MockTransport — the client stack
runs for real (auth, URL merging, JSON), only the wire is faked.

Time facts these tests lean on: 2026-06-10 is a Wednesday inside BST
(UK = UTC+1), 2026-01-15 a Thursday inside GMT (UK = UTC+0).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from autotrain.core.config import Settings
from autotrain.modules.delays.service import ArrivalReport, AssessableJourney
from autotrain.sources.hsp import HSP_BASE_URL, HspSource

SUMMER = date(2026, 6, 10)
DEP_UTC = datetime(2026, 6, 10, 9, 30, tzinfo=UTC)  # 10:30 UK wall clock
ARR_UTC = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)  # 13:00 UK wall clock
RID = "202606101234567"


def _journey(
    travel: date = SUMMER,
    dep: datetime = DEP_UTC,
    arr: datetime = ARR_UTC,
    origin: str = "MAN",
    dest: str = "EUS",
) -> AssessableJourney:
    return AssessableJourney(
        id=uuid4(),
        user_id=uuid4(),
        ticket_id=uuid4(),
        operator_id=None,
        origin_crs=origin,
        destination_crs=dest,
        travel_date=travel,
        scheduled_departure=dep,
        scheduled_arrival=arr,
        status="pending",
        price_pence=4550,
        ticket_kind="single",
        operator_min_delay_minutes=None,
    )


def _svc(ptd: str = "1030", rids: tuple[str, ...] = (RID,), toc: str = "QQ") -> dict[str, Any]:
    return {
        "serviceAttributesMetrics": {
            "origin_location": "MAN",
            "destination_location": "EUS",
            "gbtt_ptd": ptd,
            "gbtt_pta": "1300",
            "toc_code": toc,
            "rids": list(rids),
        }
    }


def _loc(crs: str = "EUS", actual_ta: str = "1310") -> dict[str, Any]:
    return {"location": crs, "gbtt_pta": "1300", "actual_ta": actual_ta, "late_canc_reason": ""}


def _hsp(
    services: list[dict[str, Any]] | None = None,
    locations: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A canned HSP: serviceMetrics answers with `services`, serviceDetails
    with `locations`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/serviceMetrics"):
            return httpx.Response(200, json={"Services": services or []})
        return httpx.Response(
            200, json={"serviceAttributesDetails": {"locations": locations or []}}
        )

    return handler


def _source(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[HspSource, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(
        transport=httpx.MockTransport(recording),
        base_url=HSP_BASE_URL,
        auth=("e@example.com", "pw"),
    )
    return HspSource(client), seen


class TestHappyPath:
    def test_late_arrival_is_reported_in_utc_with_the_toc(self) -> None:
        source, _ = _source(_hsp([_svc()], [_loc(actual_ta="1310")]))

        report = source.actual_arrival(_journey())

        # 13:10 UK summer wall clock = 12:10 UTC.
        assert report == ArrivalReport(
            actual_arrival=datetime(2026, 6, 10, 12, 10, tzinfo=UTC),
            source="hsp",
            atoc_code="QQ",
        )

    def test_speaks_hsp_wall_clock_and_day_type(self) -> None:
        source, seen = _source(_hsp([_svc()], [_loc()]))

        source.actual_arrival(_journey())

        body = json.loads(seen[0].content)
        assert body == {
            "from_loc": "MAN",
            "to_loc": "EUS",
            "from_date": "2026-06-10",
            "to_date": "2026-06-10",
            "from_time": "1027",  # 09:30 UTC as UK BST wall clock, minus tolerance
            "to_time": "1129",
            "days": "WEEKDAY",
        }
        assert seen[0].headers["Authorization"].startswith("Basic ")

    def test_winter_departure_is_gmt(self) -> None:
        source, seen = _source(_hsp([_svc(ptd="0930")], [_loc()]))
        winter = _journey(
            travel=date(2026, 1, 15),
            dep=datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
            arr=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )

        source.actual_arrival(winter)

        assert json.loads(seen[0].content)["from_time"] == "0927"  # UTC+0 in January

    def test_saturday_uses_its_own_day_type(self) -> None:
        source, seen = _source(_hsp([_svc()], [_loc()]))
        saturday = _journey(
            travel=date(2026, 6, 13),
            dep=datetime(2026, 6, 13, 9, 30, tzinfo=UTC),
            arr=datetime(2026, 6, 13, 12, 0, tzinfo=UTC),
        )

        source.actual_arrival(saturday)

        assert json.loads(seen[0].content)["days"] == "SATURDAY"

    def test_matches_departure_among_several_services(self) -> None:
        other = _svc(ptd="1045", rids=("999",))
        source, seen = _source(_hsp([other, _svc(ptd="1030")], [_loc()]))

        report = source.actual_arrival(_journey())

        assert report is not None
        assert json.loads(seen[1].content) == {"rid": RID}  # not 999

    def test_arrival_after_midnight_lands_on_the_next_day(self) -> None:
        source, _ = _source(_hsp([_svc(ptd="2330")], [_loc(actual_ta="0015")]))
        night = _journey(
            dep=datetime(2026, 6, 10, 22, 30, tzinfo=UTC),  # 23:30 UK
            arr=datetime(2026, 6, 10, 22, 55, tzinfo=UTC),  # 23:55 UK
        )

        report = source.actual_arrival(night)

        assert report is not None
        # 00:15 UK on the NEXT day = 23:15 UTC still on the travel date.
        assert report.actual_arrival == datetime(2026, 6, 10, 23, 15, tzinfo=UTC)

    def test_after_midnight_departure_queries_the_departure_day(self) -> None:
        # Ticket/timetable day is Friday, but the 00:10 service departs on
        # SATURDAY's calendar day — HSP must be asked about Saturday, with
        # Saturday's day type, or it answers about a different train.
        source, seen = _source(_hsp([_svc(ptd="0010")], [_loc(actual_ta="0100")]))
        night = _journey(
            travel=date(2026, 6, 12),  # Friday, the timetable day
            dep=datetime(2026, 6, 12, 23, 10, tzinfo=UTC),  # 00:10 UK Sat 13th
            arr=datetime(2026, 6, 12, 23, 59, tzinfo=UTC),  # 00:59 UK Sat 13th
        )

        report = source.actual_arrival(night)

        body = json.loads(seen[0].content)
        assert (body["from_date"], body["days"], body["from_time"]) == (
            "2026-06-13",
            "SATURDAY",
            "0007",
        )
        assert report is not None
        assert report.actual_arrival == datetime(2026, 6, 13, 0, 0, tzinfo=UTC)  # 01:00 UK

    def test_dst_night_crossing_is_not_off_by_an_hour(self) -> None:
        # Spring forward: dep Sat 28 Mar 23:30 UK (GMT), actual arrival 02:30
        # UK Sunday — which is BST, one hour "closer" in UTC than a naive
        # +24h bump would put it. 02:30 BST Sunday = 01:30 UTC.
        source, _ = _source(_hsp([_svc(ptd="2330")], [_loc(actual_ta="0230")]))
        dst_night = _journey(
            travel=date(2026, 3, 28),
            dep=datetime(2026, 3, 28, 23, 30, tzinfo=UTC),  # GMT: 23:30 UK
            arr=datetime(2026, 3, 28, 23, 55, tzinfo=UTC),
        )

        report = source.actual_arrival(dst_night)

        assert report is not None
        assert report.actual_arrival == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)

    def test_departure_a_minute_off_still_matches_uniquely(self) -> None:
        # The user typed 10:29 for the timetable's 10:30 — a unique near miss
        # within tolerance must match rather than age out over 7 days.
        source, _ = _source(_hsp([_svc(ptd="1030")], [_loc()]))
        one_off = _journey(dep=datetime(2026, 6, 10, 9, 29, tzinfo=UTC))  # 10:29 UK

        assert source.actual_arrival(one_off) is not None

    def test_ambiguous_near_matches_are_refused(self) -> None:
        # Two candidate trains inside the tolerance and no exact match:
        # filing against the wrong physical train is worse than deferring.
        services = [_svc(ptd="1028", rids=("111",)), _svc(ptd="1032", rids=("222",))]
        source, seen = _source(_hsp(services, [_loc()]))

        assert source.actual_arrival(_journey()) is None
        assert len(seen) == 1


class TestUnknownReturnsNone:
    def test_no_service_matches_the_departure(self) -> None:
        source, seen = _source(_hsp([_svc(ptd="1045")]))

        assert source.actual_arrival(_journey()) is None
        assert len(seen) == 1  # never asked for details

    def test_service_has_no_rid(self) -> None:
        source, _ = _source(_hsp([_svc(rids=())]))

        assert source.actual_arrival(_journey()) is None

    def test_cancelled_service_has_no_actual_arrival(self) -> None:
        source, seen = _source(_hsp([_svc()], [_loc(actual_ta="")]))

        assert source.actual_arrival(_journey()) is None
        assert len(seen) == 2  # metrics + details, then deferred

    def test_destination_absent_from_locations(self) -> None:
        source, _ = _source(_hsp([_svc()], [_loc(crs="PAD")]))

        assert source.actual_arrival(_journey()) is None

    def test_rid_less_duplicate_row_does_not_mask_the_real_one(self) -> None:
        # HSP can return two rows for one departure (portion workings); a
        # rid-less first row must not stop the search.
        source, _ = _source(_hsp([_svc(rids=()), _svc()], [_loc()]))

        assert source.actual_arrival(_journey()) is not None

    def test_impossibly_early_actual_is_deferred_not_a_day_late(self) -> None:
        # A glitched actual_ta before the departure would only "fit" by being
        # pushed ~23h into the future — that is a data error, not a delay,
        # and must not freeze a top-band entitlement.
        source, _ = _source(_hsp([_svc()], [_loc(actual_ta="0925")]))

        assert source.actual_arrival(_journey()) is None

    def test_todays_journeys_are_skipped_without_io(self) -> None:
        # HSP is next-day data; a journey still on today's UK date is a
        # guaranteed miss and must not cost an HTTP call per sweep.
        source, seen = _source(_hsp([_svc()], [_loc()]))
        dep = datetime.now(tz=UTC)
        today = _journey(travel=dep.date(), dep=dep, arr=dep + timedelta(hours=1))

        assert source.actual_arrival(today) is None
        assert len(seen) == 0

    def test_service_details_are_fetched_once_per_train(self) -> None:
        # Two users on the same delayed service: the second answer comes from
        # the cache, not a second /serviceDetails call.
        source, seen = _source(_hsp([_svc()], [_loc()]))

        assert source.actual_arrival(_journey()) is not None
        assert source.actual_arrival(_journey()) is not None

        paths = [request.url.path for request in seen]
        assert paths.count("/api/v1/serviceDetails") == 1
        assert paths.count("/api/v1/serviceMetrics") == 2


class TestFailuresRaise:
    def test_http_error_escapes_to_the_sweep(self) -> None:
        source, _ = _source(lambda request: httpx.Response(500, text="boom"))

        with pytest.raises(httpx.HTTPStatusError):
            source.actual_arrival(_journey())


class TestSettingsGuard:
    """arrivals_source=hsp without working credentials must fail at BOOT —
    a running ingestor with bad credentials 401s per journey, and per-journey
    failures are what the give-up machinery feeds on."""

    def test_hsp_without_credentials_refuses_to_boot(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgresql://x/y", arrivals_source="hsp")

    def test_blank_email_also_refuses(self) -> None:
        # An interpolated-but-unset env var arrives as '' — not None.
        with pytest.raises(ValidationError):
            Settings(
                database_url="postgresql://x/y",
                arrivals_source="hsp",
                hsp_email="",
                hsp_password=SecretStr("pw"),
            )
