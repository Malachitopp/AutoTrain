"""The identity module's public API.

Identity owns users, auth and devices (ARCHITECTURE §3). Three faces today:

* Magic-link auth (§9): request_login mints a single-use token, stores only
  its hash, and mails the raw token through an injected EmailSender;
  verify_login spends the token and answers with a user id, creating the
  account on first login. issue_session_token/session_user mint and verify
  the JWTs that deps.current_user_id now checks on every request — these
  functions replaced the api layer's X-User-Id stub.
* user_profile: who a verified session belongs to, for the api's /auth/me;
  user_is_live: whether it still belongs to anyone, for the bearer gate.
* push_targets: where a user's push notifications can be delivered, for the
  notification worker.
* Forwarding addresses (0013): forwarding_code mints the personal part of
  the address a user forwards ticket emails to; user_by_forwarding_address
  is the reverse lookup the intake webhook does on every email.
* Session cutoff (0014): session_is_live is the bearer gate's per-request
  question, answering both "still an account" and "signed out everywhere
  since this token was issued"; revoke_sessions moves the cutoff.
* Erasure (GDPR): erase_user is the one composition point — it asks each
  module to forget the person, then strips and stamps the users row.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import jwt
import psycopg
from psycopg import errors as pg_errors

# Private alias for the same reason as in the other module services: the
# module object must not be reachable through this namespace, or callers could
# climb past every import-linter contract.
from autotrain.modules.identity import repository as _repository

# Re-exported: the shapes this service hands the worker and the api layer, so
# callers stay off identity.models (the identity-privacy contract).
from autotrain.modules.identity.models import (
    ForwardingOwner,
    PushTarget,
    SessionClaims,
    UserProfile,
)
from autotrain.modules.journeys import service as _journeys

__all__ = [
    "SESSION_TTL",
    "EmailDeliveryError",
    "EmailSender",
    "LoginRateLimited",
    "PushTarget",
    "SessionClaims",
    "UserProfile",
    "account_email",
    "ensure_account",
    "erase_user",
    "forwarding_address",
    "forwarding_code",
    "issue_session_token",
    "purge_expired_login_tokens",
    "push_targets",
    "request_login",
    "revoke_sessions",
    "session_is_live",
    "session_user",
    "user_by_forwarding_address",
    "user_is_live",
    "user_profile",
    "verify_login",
]


class LoginRateLimited(RuntimeError):
    """Too many login links have been asked for; this one is refused.

    Not an error in the system — the expected answer to a caller who has had
    their share. Named by the module rather than the api layer because the
    rule is the module's: the api only decides which status code says it.
    """


class EmailDeliveryError(RuntimeError):
    """A transport could not deliver an email.

    Part of the EmailSender contract rather than any one implementation's
    private business, so the api layer can answer "the provider would not
    take it" without importing a provider, and a second transport later
    needs no new handling anywhere. Raised for both halves of a failure —
    the provider was unreachable, and the provider refused — because the
    caller's response to each is identical: the login did not go out.
    """


class EmailSender(Protocol):
    """Anything that can deliver one email.

    Implemented outside the module (sources/email.py) and injected by the
    api layer — the same seam as notifications' PushSender. Implementations
    may raise, and should raise EmailDeliveryError when they do:
    request_login runs inside the caller's transaction, so a failed send
    rolls the token insert back with it and no orphaned token outlives its
    email. They must bound their own delivery time (network timeouts) — a
    request handler is waiting on send_email.
    """

    def send_email(self, *, to: str, subject: str, body: str) -> None: ...


_TOKEN_TTL = timedelta(minutes=15)
_LOGIN_SUBJECT = "Your AutoTrain sign-in link"
# The window both login rate limits are measured over. Rolling rather than a
# calendar day on purpose: a fixed midnight boundary hands an attacker a full
# fresh budget at a time they can pick, and lets them take two budgets back to
# back across it.
_RATE_WINDOW = timedelta(hours=24)
# Public: the web session cookie's max-age is derived from it (routers/auth.py).
SESSION_TTL = timedelta(days=30)
# A verifier may run on a different host from the issuer; a few seconds of
# clock skew must not read as a token from the future.
_CLOCK_LEEWAY_SECONDS = 10


def push_targets(
    conn: psycopg.Connection, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushTarget]]:
    """Each user's registered devices, keyed by user id. Batched, like
    journeys.claim_contexts; a user with no devices is simply absent — the
    caller decides what an unreachable user means."""
    return _repository.push_targets(conn, user_ids)


def request_login(
    conn: psycopg.Connection,
    email: str,
    sender: EmailSender,
    *,
    app_base_url: str,
    per_email_daily_cap: int = 5,
    daily_cap: int = 80,
) -> None:
    """Start a magic-link login: mint an unguessable single-use token, store
    only its hash (a leaked database can recognise tokens, never mint them),
    and email the raw token — the inbox holds the only copy in existence.

    Deliberately never checks whether the email has an account. Anyone may
    request a link for any address; only the inbox's owner can use it, and
    the uniform behaviour gives an attacker no way to probe which emails
    exist here (user enumeration).

    The link is <app_base_url>/login#token=<token>: /login is the frontend's
    route, a contract with the app rather than a detail of this module.

    Raises LoginRateLimited when this inbox, or the system as a whole, has
    had its share of the window. Both caps count rows already committed,
    which is what makes them survive the api's rollback rule: a request that
    sends an email answers 204 and commits its rows, so the committed rows
    are the record of what went out, while a refused request writes nothing
    and needs to write nothing. A limiter that instead recorded its own
    refusals would have those records rolled back with the 429 that caused
    them, and would forget every attempt it ever blocked.
    """
    inbox = _email_key(email)
    _refuse_if_over_limit(conn, inbox, per_email_daily_cap=per_email_daily_cap, daily_cap=daily_cap)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + _TOKEN_TTL
    _repository.insert_login_token(conn, email, inbox, token_hash, expires_at)
    _repository.record_login_send(conn)
    sender.send_email(
        to=email,
        subject=_LOGIN_SUBJECT,
        body=_login_email_body(f"{app_base_url}/login#token={token}"),
    )


def _email_key(email: str) -> str:
    """The inbox an address reaches, which is what the per-address cap and
    erasure are really about.

    Nearly every provider delivers a+anything@x to a@x, so a cap keyed on
    the address as typed handed out a fresh budget per suffix — the cap
    that exists to stop one person being mail-bombed did not stop it. The
    reduction is the two things that are safe everywhere: the plus-suffix
    goes, and case goes (the column is citext, so case never mattered for
    equality; lower-casing keeps what is stored honest about that). Dots in
    the local part are NOT folded — that is one provider's rule, and folding
    them for everyone would merge strangers' mailboxes at the others.

    A few providers treat '+' as an ordinary character, and there two
    different mailboxes will share one budget of five a day. That is the
    right side to err on.
    """
    local, _, domain = email.partition("@")
    return f"{local.split('+', 1)[0]}@{domain}".lower()


def _refuse_if_over_limit(
    conn: psycopg.Connection, inbox: str, *, per_email_daily_cap: int, daily_cap: int
) -> None:
    """Raise LoginRateLimited if this request would exceed either cap.

    Two caps, because they stop two different things and neither covers the
    other. The per-inbox one stops a single inbox being filled with our
    mail, which is how a login endpoint gets used to attack a stranger and
    take the sending domain's reputation down with it. The daily one stops
    the provider's own quota being drained, which no per-inbox cap can do:
    an attacker uses a thousand addresses and stays under it every time.

    Checked under a transaction-scoped lock, because count-then-insert is
    otherwise a race: every request in flight at once counts under the cap
    and every one inserts, so the cap holds only up to the size of the
    connection pool. The lock serialises the check with the insert that
    follows it, and the middleware's commit or rollback releases it either
    way. Login traffic is tens a day; the serialisation costs nothing.

    Cheapest blast radius first: the per-inbox answer is the one a real
    person hits, and it is refused before the global count runs.
    """
    _repository.lock_login_requests(conn)
    since = datetime.now(UTC) - _RATE_WINDOW
    if _repository.count_login_tokens_for_inbox(conn, inbox, since, per_email_daily_cap) >= (
        per_email_daily_cap
    ):
        raise LoginRateLimited("too many login links requested")
    if _repository.count_login_sends_since(conn, since, daily_cap) >= daily_cap:
        # Deliberately the same exception, carrying nothing about which
        # cap it was. Which one you hit is a fact about other people's
        # traffic, and the api turns both into one identical 429.
        raise LoginRateLimited("too many login links requested")


def purge_expired_login_tokens(
    conn: psycopg.Connection, *, keep_days: int, batch_size: int = 500, commit_each: bool = False
) -> int:
    """Delete login tokens, and the send records beside them, older than
    `keep_days`; returns how many rows went.

    Nothing reads a token after its first 15 minutes, so this table was
    growing for ever for no one. Batched, like journeys' retention sweep, so
    a first run over a long backlog never holds one long transaction.

    The cutoff is never inside the rate limiter's window, whatever retention
    is configured. The limiter counts these same rows, so a purge that
    reached into that window would delete the evidence of what an attacker
    had already been sent and hand them a fresh budget — a hole opened by
    lowering a retention setting, with nothing at either site to suggest the
    two were connected. Taking the max here makes that impossible rather
    than merely documented.
    """
    before = datetime.now(UTC) - max(timedelta(days=keep_days), _RATE_WINDOW)
    deleted = 0
    for purge in (
        lambda: _repository.delete_old_login_tokens(conn, before, batch_size),
        lambda: _repository.delete_old_login_sends(conn, before, batch_size),
    ):
        while True:
            with conn.transaction():
                batch = purge()
            deleted += batch
            if commit_each:
                conn.commit()
            if batch < batch_size:
                break
    return deleted


def _login_email_body(link: str) -> str:
    """The words wrapped around the link.

    The body used to be the bare URL and nothing else. That was right while
    the log was the inbox, and wrong the moment a real one is: a message
    whose entire content is one long URL is among the oldest phishing shapes
    there is, and spam filters score it accordingly — this is the one email
    in the product that absolutely must arrive.

    So: what it is, the link alone on its own line where every client will
    linkify it, how long it lasts, and what to do if the reader did not ask
    for it. The expiry is read from _TOKEN_TTL rather than written out, so
    the promise in the inbox cannot drift from the one the database keeps.
    """
    minutes = int(_TOKEN_TTL.total_seconds() // 60)
    # Joined rather than concatenated so the shape of the message is the
    # shape of this list: one entry per line, blank entries where the
    # blank lines go, and the link on a line of its own.
    return "\n".join(
        [
            "Open this link to sign in to AutoTrain:",
            "",
            link,
            "",
            f"It signs you in once and stops working after {minutes} minutes.",
            "",
            "If you did not ask to sign in, you can ignore this email. Nobody",
            "can sign in as you without the link above, and nothing has changed",
            "on your account.",
            "",
        ]
    )


def verify_login(conn: psycopg.Connection, token: str) -> UUID | None:
    """Exchange a clicked magic-link token for a user id — None if the token
    is unknown, expired, or already spent. The guarded UPDATE judges all
    three at once and race-safely: two concurrent clicks on one link log in
    exactly one caller. First login creates the account — signup and login
    are deliberately the same act, and clicking the link is what proves the
    address is real.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    email = _repository.spend_login_token(conn, token_hash)
    if email is None:
        return None
    return ensure_account(conn, email)


def ensure_account(conn: psycopg.Connection, email: str) -> UUID:
    """The account for this address, created if there is not one yet.

    First sight of an address IS the sign-up — there is no separate
    registration step anywhere in this system, because a magic link proves
    the same thing a sign-up form would and asks for less. verify_login is
    one caller; a mailbox poll is the other, and it has the same problem:
    it holds an address and needs the account behind it.

    Not race-guarded, deliberately. Both callers are already serialised by
    something stronger than a lock — a login token can be spent exactly
    once, and the poll runs under the scheduler's advisory job lock — so a
    unique-violation retry here would be code that could never run. If a
    third caller ever appears without that property, this is where the
    retry goes.
    """
    user_id = _repository.user_id_by_email(conn, email)
    if user_id is None:
        user_id = _repository.create_user(conn, email)
    return user_id


def issue_session_token(user_id: UUID, *, secret: str, now: datetime | None = None) -> str:
    """Issue a session JWT: who (sub), when it was issued (iat, what the
    per-user cutoff is judged against), when it dies (exp), and what it is
    (typ, so a future refresh token can never pass as an access token).
    `now` is for tests that need a token from a chosen moment."""
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": issued,
        "exp": issued + SESSION_TTL,
        "typ": "access",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def session_user(encoded_token: str, *, secret: str) -> SessionClaims | None:
    """The claims of a session token, or None if it is not one of ours: bad
    signature, expired, missing a claim, or not an access token. Only the
    signature and shape are judged here; whether the account and the
    cutoff still allow it is session_is_live's database question."""
    try:
        payload = jwt.decode(
            encoded_token,
            secret,
            algorithms=["HS256"],
            leeway=_CLOCK_LEEWAY_SECONDS,
            options={"require": ["sub", "exp", "iat"]},
        )
        if payload.get("typ") != "access":
            return None
        return SessionClaims(
            user_id=UUID(payload["sub"]),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        )
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        return None


def user_profile(conn: psycopg.Connection, user_id: UUID) -> UserProfile | None:
    """The signed-in user's profile — None if the account is unknown or was
    erased after the session was issued."""
    return _repository.user_profile(conn, user_id)


def account_email(conn: psycopg.Connection, user_id: UUID) -> str | None:
    """The address a live account can be reached at, or None once it is
    erased or was never there.

    Distinct from user_profile in what it is FOR: a caller that wants to
    send a person something needs one string, and asking for the whole
    profile to reach it would copy the rest of their record through a worker
    that has no use for it.
    """
    return _repository.user_email(conn, user_id)


def user_is_live(conn: psycopg.Connection, user_id: UUID) -> bool:
    """Whether a verified session still names a usable account — False once
    the user is erased (GDPR) or was never created. A session outlives
    erasure by up to _SESSION_TTL, so the bearer gate asks this on every
    request: the ghost is refused everywhere, not only where a handler
    happens to load the user. One primary-key lookup per request."""
    return _repository.user_is_live(conn, user_id)


# tickets-<code>@<domain>. Codes are lowercase hex; the match is
# case-insensitive because mail systems may fold the local part either way.
_FORWARDING_ADDRESS = re.compile(r"^tickets-([a-z0-9]{6,64})@", re.IGNORECASE)
# 128 bits. Nothing in a forwarded email proves who forwarded it
# (journeys.intake's note on the two shapes), so this code IS the
# credential, and it is not a secret that stays hidden: it appears in the
# headers of every message that touches it, in the user's own mail settings,
# and in any header dump they paste into a support thread. Sized so guessing
# is hopeless, and rotatable for when it leaks anyway.
_FORWARDING_CODE_BYTES = 16


def forwarding_code(conn: psycopg.Connection, user_id: UUID) -> str | None:
    """The user's forwarding code, minted on first call and stable after.
    None for an unknown or erased user.

    Minting is a guarded UPDATE: two first calls racing both end with the one
    code that won. A collision with another user's code (a unique index, one
    in a trillion) rolls back its savepoint and tries again.
    """
    for _ in range(3):
        row = _repository.forwarding_code(conn, user_id)
        if row is None:
            return None
        if row.forwarding_code is not None:
            return row.forwarding_code
        code = secrets.token_hex(_FORWARDING_CODE_BYTES)
        try:
            with conn.transaction():
                if _repository.claim_forwarding_code(conn, user_id, code):
                    return code
        except pg_errors.UniqueViolation:
            continue
    raise RuntimeError(f"could not mint a forwarding code for user {user_id}")


def rotate_forwarding_code(conn: psycopg.Connection, user_id: UUID) -> str | None:
    """A fresh code in place of the old one, for an address that has leaked
    or is being spammed: from now on mail to the old address is nobody's
    (the webhook stores it as 'unknown recipient', without a body). The
    user updates their forwarding rule; nothing else changes. None for an
    unknown or erased user. Same collision retry as minting."""
    for _ in range(3):
        code = secrets.token_hex(_FORWARDING_CODE_BYTES)
        try:
            with conn.transaction():
                return code if _repository.rotate_forwarding_code(conn, user_id, code) else None
        except pg_errors.UniqueViolation:
            continue
    raise RuntimeError(f"could not rotate the forwarding code for user {user_id}")


def forwarding_address(code: str, domain: str) -> str:
    """The address the user forwards ticket emails to."""
    return f"tickets-{code}@{domain}"


def user_by_forwarding_address(conn: psycopg.Connection, address: str) -> ForwardingOwner | None:
    """The account a forwarding address belongs to (id and email) — None for
    any address that is not one of ours, or whose code no live account
    holds."""
    match = _FORWARDING_ADDRESS.match(address.strip())
    if match is None:
        return None
    return _repository.user_by_forwarding_code(conn, match.group(1).lower())


def session_is_live(conn: psycopg.Connection, *, user_id: UUID, issued_at: datetime) -> bool:
    """The bearer gate's one database read: False once the account is erased
    (or never existed), and False for a session issued before the user's
    cutoff — the moment they last signed out everywhere. A token issued in
    the same second as the cutoff passes: iat is whole seconds, and the
    user signing straight back in must not be refused."""
    gate = _repository.session_gate(conn, user_id)
    if gate is None:
        return False
    cutoff = gate.sessions_invalid_before
    return cutoff is None or issued_at >= cutoff


def revoke_sessions(conn: psycopg.Connection, user_id: UUID) -> bool:
    """Sign out everywhere: every session issued before now is refused from
    here on. False for an unknown or erased account."""
    return _repository.revoke_sessions(conn, user_id)


def erase_user(conn: psycopg.Connection, user_id: UUID) -> bool:
    """Erase a person (GDPR), keeping what must be kept.

    0004's rule is anonymise, not delete: claims filed on the user's behalf
    stay for audit and dispute (their FKs are RESTRICT), so the users row
    survives with every identifying column nulled and deleted_at set. What
    goes, in order: everything the journeys module holds that came from the
    person (forwarded emails, seller and email references on tickets — see
    journeys.forget_user); pending login tokens for the email; devices; then
    the email, name, forwarding code, password and every session.

    False if there was no live account to erase — a second call is a no-op.
    tests/test_erasure.py scans every text column afterwards; a migration
    that adds a place personal data lives must extend one of the forget
    steps or that test fails.
    """
    email = _repository.user_email(conn, user_id)
    if email is None:
        return False
    _journeys.forget_user(conn, user_id)
    _repository.delete_login_tokens(conn, _email_key(email))
    _repository.delete_devices(conn, user_id)
    return _repository.erase_user_row(conn, user_id)
