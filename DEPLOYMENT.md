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

## 5. Settings that must change from their defaults

`AUTOTRAIN_ENVIRONMENT=production` refuses to boot while several development
settings remain (`core/config.py`, `_production_lockdown`), which covers the
JWT secret, the cookie's Secure flag, non-https origins, and a `log` or
`none` email sender. It cannot check what it cannot see, so also confirm:

* `AUTOTRAIN_RESEND_API_KEY` and `AUTOTRAIN_EMAIL_FROM` on a verified domain
* `AUTOTRAIN_INTAKE_SECRET` matching whatever posts to `/intake/email`
* `AUTOTRAIN_ANTHROPIC_API_KEY` if `AUTOTRAIN_TICKET_EXTRACTOR=claude`
* `AUTOTRAIN_HSP_EMAIL` / `AUTOTRAIN_HSP_PASSWORD` if the ingestor is running
