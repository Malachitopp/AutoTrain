"""HSP arrivals source — the first real implementation of
delays.service.ArrivalsSource.

HSP (Historical Service Performance) is National Rail's free API over what
actually happened to timetabled services: planned vs actual times per calling
point, next-day data. Auth is HTTP Basic with a free Rail Data Portal
account. Two calls answer one journey:

    serviceMetrics  — which service was this? (corridor + date + departure)
    serviceDetails  — what happened to it at the destination?

Placement note: sources/ sits outside modules/ on purpose. The delay engine
defines the protocol and stays transport-ignorant; entrypoints construct a
source and hand it in. ("adapters" is deliberately NOT the name — ARCHITECTURE
§6 reserves that word for the claims-filing side.)

Time handling: HSP speaks UK wall-clock strings ("0930"); the schema speaks
UTC. Every conversion is anchored to the DEPARTURE's UK calendar day — not
journeys.travel_date, which is the timetable day and lags a calendar day for
services departing after midnight (0005's design note).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from autotrain.modules.delays.service import ArrivalReport, AssessableJourney

logger = logging.getLogger(__name__)

HSP_BASE_URL = "https://hsp-prod.rockshore.net/api/v1"

_LONDON = ZoneInfo("Europe/London")

# HSP partitions the timetable by day type; weekday() 5/6 are Sat/Sun.
_DAY_TYPES = {5: "SATURDAY", 6: "SUNDAY"}

# A user-entered departure may be a minute or two off the public timetable
# (gbtt_ptd). Accept a unique near miss within this many minutes; refuse
# ambiguity — filing against the wrong physical train is worse than filing
# nothing.
_MATCH_TOLERANCE_MINUTES = 3

# An actual arrival that can only be reconciled by being ~a day late is a
# data glitch (corrupt/early actual_ta pushed over the midnight bump), not a
# delay. Nothing on the UK network is genuinely 12h+ late without being
# cancelled outright.
_MAX_PLAUSIBLE_LATENESS = timedelta(hours=12)

# serviceDetails responses are historical fact once actuals are published;
# cache them so K users on one delayed train cost one fetch, not K.
_DETAILS_CACHE_MAX = 2048


class HspSource:
    """One instance per process; holds the HTTP client for its lifetime.

    Every public failure mode maps onto the ArrivalsSource contract: unknown
    journey → None (the sweep retries until the give-up window); transport
    or parse failure → raise (the sweep isolates it to this journey).
    """

    def __init__(self, client: httpx.Client) -> None:
        # Injected so tests drive the source through httpx.MockTransport;
        # production construction goes through from_credentials.
        self._client = client
        self._locations_cache: dict[str, list[dict[str, Any]]] = {}

    @classmethod
    def from_credentials(
        cls, email: str, password: str, *, timeout_seconds: float = 30.0
    ) -> HspSource:
        return cls(
            httpx.Client(
                base_url=HSP_BASE_URL,
                auth=(email, password),
                timeout=timeout_seconds,
            )
        )

    def close(self) -> None:
        self._client.close()

    def actual_arrival(self, journey: AssessableJourney) -> ArrivalReport | None:
        dep_local = journey.scheduled_departure.astimezone(_LONDON)
        service_day = dep_local.date()

        # HSP is next-day data: asking about today's services is a guaranteed
        # miss the sweep would otherwise repeat every interval all day.
        if service_day >= datetime.now(tz=UTC).astimezone(_LONDON).date():
            return None

        found = self._find_service(journey, dep_local)
        if found is None:
            return None
        rid, toc_code = found
        arrival = self._destination_arrival(rid, journey, service_day)
        if arrival is None:
            return None
        return ArrivalReport(actual_arrival=arrival, source="hsp", atoc_code=toc_code)

    def _find_service(
        self, journey: AssessableJourney, dep_local: datetime
    ) -> tuple[str, str | None] | None:
        """Match the journey to a timetabled service on the DEPARTURE's UK
        calendar day. Exact gbtt_ptd match wins; else a unique near miss
        within the tolerance; else None."""
        service_day = dep_local.date()
        wanted = dep_local.hour * 60 + dep_local.minute

        # Window: tolerance before the wanted departure to 59 minutes after,
        # clamped to the day edges — HSP windows do not wrap days.
        start = dep_local - timedelta(minutes=_MATCH_TOLERANCE_MINUTES)
        from_time = "0000" if start.date() != service_day else start.strftime("%H%M")
        end = dep_local + timedelta(minutes=59)
        to_time = "2359" if end.date() != service_day else end.strftime("%H%M")

        response = self._client.post(
            "/serviceMetrics",
            json={
                "from_loc": journey.origin_crs,
                "to_loc": journey.destination_crs,
                "from_date": service_day.isoformat(),
                "to_date": service_day.isoformat(),
                "from_time": from_time,
                "to_time": to_time,
                "days": _DAY_TYPES.get(service_day.weekday(), "WEEKDAY"),
            },
        )
        response.raise_for_status()

        candidates: list[tuple[int, str, str | None]] = []
        for svc in response.json().get("Services") or []:
            attrs: dict[str, Any] = svc.get("serviceAttributesMetrics") or {}
            ptd = attrs.get("gbtt_ptd") or ""
            if len(ptd) != 4 or not ptd.isdigit():
                continue
            diff = abs(int(ptd[:2]) * 60 + int(ptd[2:]) - wanted)
            if diff > _MATCH_TOLERANCE_MINUTES:
                continue
            # A rid-less row (portion working, duplicate metrics row) must
            # not mask a sibling row for the same departure that has one.
            rids = attrs.get("rids") or []
            if not rids:
                continue
            candidates.append((diff, rids[0], attrs.get("toc_code") or None))

        exact = [c for c in candidates if c[0] == 0]
        if exact:
            return exact[0][1], exact[0][2]
        if len(candidates) == 1:
            diff, rid, toc = candidates[0]
            logger.info(
                "hsp: matched %s->%s within %d min of entered departure",
                journey.origin_crs,
                journey.destination_crs,
                diff,
            )
            return rid, toc
        if candidates:
            logger.warning(
                "hsp: %d near-matches for %s->%s dep %s — ambiguous, refusing",
                len(candidates),
                journey.origin_crs,
                journey.destination_crs,
                dep_local.strftime("%H%M"),
            )
        return None

    def _destination_arrival(
        self, rid: str, journey: AssessableJourney, service_day: date
    ) -> datetime | None:
        for location in self._locations(rid):
            if location.get("location") != journey.destination_crs:
                continue
            hhmm = location.get("actual_ta")
            if not hhmm:
                # No recorded arrival: cancelled, diverted, or data gap
                # (late_canc_reason says which). Defer rather than guess —
                # pricing a cancellation is claims-module policy, not a
                # delay measurement.
                logger.info("hsp: rid %s has no actual arrival at %s", rid, journey.destination_crs)
                return None
            return self._arrival_instant(hhmm, journey, service_day)
        return None

    def _locations(self, rid: str) -> list[dict[str, Any]]:
        cached = self._locations_cache.get(rid)
        if cached is not None:
            return cached
        response = self._client.post("/serviceDetails", json={"rid": rid})
        response.raise_for_status()
        attrs = response.json().get("serviceAttributesDetails") or {}
        locations: list[dict[str, Any]] = attrs.get("locations") or []
        # Cache only once actuals exist: an unpublished service must stay
        # re-queryable, but published history is immutable.
        if any(loc.get("actual_ta") for loc in locations):
            if len(self._locations_cache) >= _DETAILS_CACHE_MAX:
                self._locations_cache.clear()
            self._locations_cache[rid] = locations
        return locations

    @staticmethod
    def _arrival_instant(
        hhmm: str, journey: AssessableJourney, service_day: date
    ) -> datetime | None:
        """'1310' as UK wall clock → aware UTC, on the service day or (for a
        service that ran past midnight) the day after.

        Each candidate day is localized separately, so the UK offset is
        re-resolved per day — a bare +24h would be an hour off across both
        DST transition nights. A time that fits neither day plausibly is a
        data glitch: freeze nothing, defer.
        """
        t = time(int(hhmm[:2]), int(hhmm[2:]))
        for day_offset in (0, 1):
            naive = datetime.combine(service_day + timedelta(days=day_offset), t)
            candidate = naive.replace(tzinfo=_LONDON).astimezone(UTC)
            if candidate < journey.scheduled_departure:
                continue  # arrived before it left — must be the next day
            if candidate - journey.scheduled_arrival > _MAX_PLAUSIBLE_LATENESS:
                logger.warning(
                    "hsp: actual_ta %r for journey %s only fits ~a day late — "
                    "treating as a data glitch and deferring",
                    hhmm,
                    journey.id,
                )
                return None
            return candidate
        return None
