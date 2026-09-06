"""API tests for the intake routes: the provider-facing webhook, and the
user-facing forwarding address and email list. Same harness as the other
API suites (conftest.client). The forwarding-code rules of the identity
module are asserted here too, through the address route they exist for."""

from __future__ import annotations

import re
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from autotrain.api.routers import intake as intake_router
from autotrain.core.config import get_settings
from autotrain.modules.identity import service as identity
from autotrain.modules.journeys import service as journeys
from conftest import TEST_INBOUND_DOMAIN, TEST_INTAKE_SECRET, auth_header, mk_user

SECRET_HEADER = {"X-AutoTrain-Intake-Secret": TEST_INTAKE_SECRET}
BODY = "Your e-ticket: Leeds to London Kings Cross, 12 Sep 2026, 09:15 - 11:31. Paid £62.00."


def _email(recipient: str, message_id: str = "<m1@retailer.test>") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "sender": "tickets@retailer.test",
        "recipient": recipient,
        "subject": "Your e-ticket",
        "body": BODY,
    }


def _address_for(conn: psycopg.Connection, user_id: Any) -> str:
    code = identity.forwarding_code(conn, user_id)
    assert code is not None
    return identity.forwarding_address(code, TEST_INBOUND_DOMAIN)


def _stored(conn: psycopg.Connection, message_id: str) -> tuple | None:
    return conn.execute(
        "SELECT user_id, status, body FROM inbound_emails WHERE message_id = %s", (message_id,)
    ).fetchone()


class TestWebhook:
    def test_the_secret_is_the_only_credential(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        user_id = mk_user(conn)
        payload = _email(_address_for(conn, user_id))

        assert client.post("/intake/email", json=payload).status_code == 401
        wrong = {"X-AutoTrain-Intake-Secret": "not-the-secret"}
        assert client.post("/intake/email", json=payload, headers=wrong).status_code == 401

        resp = client.post("/intake/email", json=payload, headers=SECRET_HEADER)
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"status": "received"}
        assert _stored(conn, "<m1@retailer.test>") == (user_id, "received", BODY)

    def test_repeats_and_strangers_get_a_202_that_says_so(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        """A provider retries on 4xx/5xx, and neither case is fixed by a
        retry: so 202, with the receipt saying what happened."""
        address = _address_for(conn, mk_user(conn))
        assert client.post("/intake/email", json=_email(address), headers=SECRET_HEADER).json() == {
            "status": "received"
        }
        assert client.post("/intake/email", json=_email(address), headers=SECRET_HEADER).json() == {
            "status": "duplicate"
        }

        stranger = _email(f"tickets-0000000000@{TEST_INBOUND_DOMAIN}", "<m2@retailer.test>")
        resp = client.post("/intake/email", json=stranger, headers=SECRET_HEADER)
        assert resp.json() == {"status": "rejected"}
        # Recorded, but the body of a stranger's email is not kept.
        assert _stored(conn, "<m2@retailer.test>") == (None, "rejected", "")

    def test_an_unconfigured_secret_is_a_503_not_an_open_door(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        settings = get_settings().model_copy(update={"intake_secret": None})
        monkeypatch.setattr(intake_router, "get_settings", lambda: settings)
        resp = client.post("/intake/email", json=_email("x@y.test"), headers=SECRET_HEADER)
        assert resp.status_code == 503


class TestForwardingAddress:
    def test_is_minted_once_and_routes_back_to_its_user(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        user_id = mk_user(conn)
        headers = auth_header(user_id)

        first = client.get("/intake/address", headers=headers)
        assert first.status_code == 200, first.text
        address = first.json()["address"]
        assert re.fullmatch(rf"tickets-[0-9a-f]{{10}}@{re.escape(TEST_INBOUND_DOMAIN)}", address)
        # Stable: the same code every time, never a new one.
        assert client.get("/intake/address", headers=headers).json()["address"] == address
        # Distinct per user.
        other = auth_header(mk_user(conn, "other@example.com"))
        assert client.get("/intake/address", headers=other).json()["address"] != address

        # The reverse lookup the webhook does, including the ways mail
        # systems mangle an address.
        assert identity.user_by_forwarding_address(conn, address) == user_id
        assert identity.user_by_forwarding_address(conn, address.upper()) == user_id
        assert identity.user_by_forwarding_address(conn, f"  {address} ") == user_id
        assert (
            identity.user_by_forwarding_address(conn, f"tickets-ffffffffff@{TEST_INBOUND_DOMAIN}")
            is None
        )
        assert identity.user_by_forwarding_address(conn, "someone@example.com") is None

    def test_needs_a_session_and_a_configured_domain(
        self, client: TestClient, conn: psycopg.Connection, monkeypatch: Any
    ) -> None:
        assert client.get("/intake/address").status_code == 401
        settings = get_settings().model_copy(update={"inbound_email_domain": None})
        monkeypatch.setattr(intake_router, "get_settings", lambda: settings)
        resp = client.get("/intake/address", headers=auth_header(mk_user(conn)))
        assert resp.status_code == 503


class TestListEmails:
    def test_lists_the_users_own_emails_without_their_bodies(
        self, client: TestClient, conn: psycopg.Connection
    ) -> None:
        alice = mk_user(conn, "alice@example.com")
        bob = mk_user(conn, "bob@example.com")
        for user_id, message_id in ((alice, "<1@r>"), (alice, "<2@r>"), (bob, "<3@r>")):
            journeys.receive_ticket_email(
                conn,
                user_id=user_id,
                message_id=message_id,
                sender="tickets@retailer.test",
                recipient="tickets-x@in.autotrain.test",
                subject=f"Booking {message_id}",
                body=BODY,
            )

        resp = client.get("/intake/emails", headers=auth_header(alice))

        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        assert {item["subject"] for item in items} == {"Booking <1@r>", "Booking <2@r>"}
        assert set(items[0]) == {
            "id",
            "received_at",
            "sender",
            "subject",
            "status",
            "status_reason",
        }
        assert all(item["status"] == "received" for item in items)
