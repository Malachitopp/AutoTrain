"""API tests for GET /operators: the public list of operators AutoTrain can
file a claim with.

"Supported" is the claims module's word for `adapter <> 'none' AND is_active`.
This is the first endpoint outside /auth that takes no bearer token — the
landing page shows the list before anyone has signed in — so no test here
sends an Authorization header. Same harness as the other API suites
(conftest.client: the real app over the rollback conn).
"""

from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

# The nine operators 0011 gave a deep link. Everything else in the 0008 seed
# has adapter 'none' and must never appear.
SUPPORTED = {"NT", "VT", "GR", "SW", "TL", "GN", "SN", "SE", "GW"}


def _codes(client: TestClient) -> list[str]:
    resp = client.get("/operators")
    assert resp.status_code == 200, resp.text
    return [item["atoc_code"] for item in resp.json()]


class TestListOperators:
    def test_lists_exactly_the_nine_with_a_filing_link(self, client: TestClient) -> None:
        codes = _codes(client)
        assert len(codes) == 9
        assert set(codes) == SUPPORTED

    def test_needs_no_session(self, client: TestClient) -> None:
        # No test in this file sends a bearer token; this one names the fact.
        # A garbage token must not hurt either: the endpoint never reads the
        # header, so it cannot turn a public page into a 401.
        resp = client.get("/operators", headers={"Authorization": "Bearer not-a-token"})
        assert resp.status_code == 200

    def test_sorted_by_name(self, client: TestClient) -> None:
        names = [item["name"] for item in client.get("/operators").json()]
        # Seed order would put Northern (NT) first; ORDER BY name puts Avanti
        # first and Thameslink last. The middle is left to Postgres's
        # collation (locales disagree on how a space sorts), so only pairs
        # every collation agrees on are pinned.
        assert names[0] == "Avanti West Coast"
        assert names[-1] == "Thameslink"
        assert names.index("Great Northern") < names.index("Great Western Railway")

    def test_each_item_carries_the_three_public_fields(self, client: TestClient) -> None:
        items = {item["atoc_code"]: item for item in client.get("/operators").json()}
        # Exactly these three: no id, no claim_url, nothing internal leaks
        # onto a public page.
        assert items["GR"] == {"atoc_code": "GR", "name": "LNER", "min_delay_minutes": 30}
        assert items["NT"]["min_delay_minutes"] == 15

    def test_switched_off_operator_drops_out(
        self, conn: psycopg.Connection, client: TestClient
    ) -> None:
        # is_active is the switch for an operator that has stopped taking
        # claims (a franchise change — OperatorFiling's docstring). Flipping
        # it must hide the operator at once, with no deploy.
        conn.execute("UPDATE operators SET is_active = false WHERE atoc_code = 'GW'")
        codes = _codes(client)
        assert "GW" not in codes
        assert set(codes) == SUPPORTED - {"GW"}

    def test_operator_without_an_adapter_never_appears(
        self, conn: psycopg.Connection, client: TestClient
    ) -> None:
        # Active, but adapter 'none': nowhere to send the person, so not
        # supported. A fresh row rather than a seeded one, so the test shows
        # the rule and not just the seed.
        conn.execute(
            "INSERT INTO operators "
            "(atoc_code, name, min_delay_minutes, claim_window_days, adapter) "
            "VALUES ('QQ', 'Test Railways', 15, 28, 'none')"
        )
        assert "QQ" not in _codes(client)
