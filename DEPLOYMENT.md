# Deployment

Things that are true about the running system rather than the code, and that
the code therefore cannot enforce. Each one here exists because the
alternative was a comment in a source file that nobody would read at the
moment it mattered.

## 1. Per-caller rate limiting belongs at the edge

**Required before the API is public.**

The application does not rate limit by caller, on purpose. A middleware that
did was written and removed (see the note in `api/app.py`): the application
cannot tell who is calling. It sees whichever socket or header the deployment
hands it, so the same code gave one machine 2^64 buckets over an IPv6 /64 and
gave every user in the world a single shared bucket once a proxy sat in
front. That is a fact about position on the network, not about the code.

What the application *does* bound is its own spend — `identity.request_login`
caps links per inbox and sends per day, counted from committed rows.

At the edge, with the API behind Cloudflare:

* **A Managed Challenge on `POST /auth/login/request`.** WAF → Custom rules.
  The free plan allows 5 custom rules and every action except Log, so this
  costs nothing. Prefer it over a rate limiting rule: a challenge asks
  "are you a browser", which a real person passes silently and a script
  fails, and no request threshold is generous enough for humans and tight
  enough for bots at the same time.
* **A rate limiting rule as a crude flood stop**, if wanted. Note its free
  tier is weak: one rule, keyed on IP only, with a counting window of 10
  seconds (60 at most). A caller pacing itself under that window sustains
  tens of thousands of requests a day, so this does NOT bound the daily
  email spend. The database caps do that.

## 2. The origin must not be reachable except through the edge

**Required, and it is what makes item 1 mean anything.**

Edge protection only works if the edge cannot be skipped. An origin with a
public IP can be connected to directly, and then no WAF rule has ever seen
the request. Either:

* restrict the origin's firewall to Cloudflare's published IP ranges, or
* use Cloudflare Tunnel, so the origin has no public inbound address at all.

## 3. DNS: what must never be touched

`malachitopp.com` receives real personal mail. As of 2026-09-07 its apex MX
records point at Zoho (`mx.zoho.eu`, `mx2.zoho.eu`, `mx3.zoho.eu`) with
`v=spf1 include:zohomail.eu ~all`.

**Never add or change MX records on the apex.** Every AutoTrain mail record
lives on a subdomain:

| Purpose | Subdomain | Provider |
| --- | --- | --- |
| Sending login links | `mail.malachitopp.com` | Resend (records under `send.mail` and `resend._domainkey.mail`) |
| Receiving forwarded tickets | `in.malachitopp.com` | Cloudflare Email Routing (not yet configured) |

Cloudflare Email Routing's onboarding flow is written for the apex and has a
history of proposing to replace existing MX records. Enter through the
apex domain's settings → **Subdomains**, and if any screen shows a change to
`malachitopp.com` itself, stop.

A DMARC record, if added, goes on the sending subdomain
(`_dmarc.mail.malachitopp.com`) and not the apex, so it cannot change how
receivers judge the Zoho mail.

## 4. Migrations run before the new code serves traffic

The API reads columns that migrations create. A deploy that starts the new
image against an un-migrated database answers 500 on every affected route —
this has already happened once in development
(`column "sessions_invalid_before" does not exist`, migrations 13–16
pending). Run `autotrain-migrate up` to completion as a release step, before
any new task accepts a request.

## 5. The mailbox password is a mailbox password

**Required if `AUTOTRAIN_MAILBOX_SOURCE=imap`.**

`AUTOTRAIN_IMAP_PASSWORD` grants read access to every email in the account,
not just the ticket ones. It sits at the same level as the JWT secret: never
committed, never in a shell history, and set only where the scheduler runs.

For Gmail it must be an **App Password**, not the account password — Google
refuses the account password over IMAP, and will only issue an App Password
once two-factor is on (<https://myaccount.google.com/apppasswords>). Revoking
one is a single click there, which is the reason to prefer it beyond the fact
that nothing else works.

Narrow what it can see anyway. A Gmail filter that labels booking
confirmations, with `AUTOTRAIN_IMAP_FOLDER` pointing at that label, means the
poll never lists anything else. IMAP has no per-folder credential, so this is
a limit on what is read, not on what could be — but it is the difference
between a bug touching your tickets and a bug touching your bank mail.

The mailbox is opened read-only (`EXAMINE`) and every fetch uses `BODY.PEEK`,
so nothing is marked read, moved or deleted. That is enforced in
`sources/imap_mailbox.py` and asserted in its tests; it is listed here because
it is the promise that makes pointing this at a personal inbox reasonable.

## 6. Turn the mailbox poll off before erasing its owner

**Required if `AUTOTRAIN_MAILBOX_SOURCE=imap` and you ever run erasure.**

GDPR erasure anonymises rather than deletes: `users.email` is set to NULL and
the row is stamped (`0004`). The mailbox poll, though, resolves its owner by
address on every pass — it asks `identity.ensure_account` for the account
behind `AUTOTRAIN_MAILBOX_OWNER_EMAIL`, and creates one if there is none.

After erasure there is none, because the address it matched on is gone. So
the next pass creates a fresh account and re-imports every message still
inside the lookback window, under a new id. Erasure is undone within one
scheduler interval, silently.

Nothing in the code can tell "this address was erased, leave it alone" from
"this address is new" — the record that would say so is exactly what erasure
removes. So it is an operational rule instead: **stop the scheduler, or set
`AUTOTRAIN_MAILBOX_SOURCE=none`, before erasing the mailbox owner.**

## 7. Settings that must change from their defaults

`AUTOTRAIN_ENVIRONMENT=production` refuses to boot while several development
settings remain (`core/config.py`, `_production_lockdown`), which covers the
JWT secret, the cookie's Secure flag, non-https origins, and a `log` or
`none` email sender. It cannot check what it cannot see, so also confirm:

* `AUTOTRAIN_RESEND_API_KEY` and `AUTOTRAIN_EMAIL_FROM` on a verified domain
* `AUTOTRAIN_INTAKE_SECRET` matching whatever posts to `/intake/email`
* `AUTOTRAIN_ANTHROPIC_API_KEY` if `AUTOTRAIN_TICKET_EXTRACTOR=claude`
* `AUTOTRAIN_HSP_EMAIL` / `AUTOTRAIN_HSP_PASSWORD` if the ingestor is running
* the four `AUTOTRAIN_IMAP_*` / `AUTOTRAIN_MAILBOX_OWNER_EMAIL` settings if
  the mailbox poll is running (Settings refuses to boot without all four)
* at least one delivery channel for the worker: `AUTOTRAIN_PUSH_SENDER`,
  `AUTOTRAIN_EMAIL_SENDER`, or both. With neither it refuses to start, which
  is deliberate — the sweep stamps every detection it examines, so a worker
  with nowhere to deliver would silently mark a month of real money as
  told-about
