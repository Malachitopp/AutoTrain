# AutoTrain — MVP Plan

Auto-refund app for late UK trains: detect the delay, file the Delay Repay claim, track the payout. The user does nothing except get their money.

*Drafted 13 Aug 2026.*

---

## 1. The product in one paragraph

Most UK rail passengers never claim the compensation they're legally owed under Delay Repay, because claiming means noticing the delay, finding the right operator's form, digging out the ticket, and filling everything in within 28 days. AutoTrain removes all of that. The user adds their ticket (scan, email forward, or manual entry), AutoTrain watches the journey against live rail data, and when the train arrives 15+ minutes late it prepares and submits the claim to the operator. The refund is paid by the operator directly to the user's bank/card — AutoTrain never holds the money (see §6).

## 2. How Delay Repay actually works (constraints we design around)

Delay Repay is a per-operator scheme, not a national system. Nearly all operators pay on a sliding scale — typically 25% of the single fare at 15–29 min late, 50% at 30–59 min, 100% at 60–119 min, and 100% of a return at 120+ min. Claims must usually be filed within 28 days. Crucially:

- **There is no public claims API.** Every operator has its own web form. Third parties (including Trainline) either pre-fill these forms or automate them.
- **A few operators auto-refund already** (e.g. GWR Advance tickets bought direct, Thameslink Key Smartcard journeys), but automatic schemes only cover tickets bought directly from that operator in specific formats. Third-party purchases, split tickets, multi-operator journeys, missed connections, and paper tickets all fall through the cracks — that gap is AutoTrain's market.
- **Payouts go to the passenger** via bank transfer, card refund, or vouchers, handled by the operator.

## 3. MVP scope

**In:**

1. Add a journey: scan an e-ticket barcode / enter journey details manually (origin, destination, date, operator, ticket price). Email-forwarding of ticket confirmations is a fast follow.
2. Track the journey against live data and detect qualifying delays automatically.
3. Notify the user: "You're owed £6.40 (50%) for your 08:14 to Euston."
4. File the claim: v1 opens the operator's Delay Repay form pre-filled wherever possible (deep link + autofill), with server-side automated submission for the 3–5 biggest operators as the headline feature.
5. Track claim status and payout, with a running "money recovered" total.

**Out (for now):** holding user funds in-app, season ticket/smartcard claims, non-UK rail, missed-connection logic, split-ticket optimisation.

## 4. Data feeds (all free, via the Rail Data Marketplace)

| Feed | What it gives us | Use |
|---|---|---|
| Darwin LDB API (JSON) | Live arrival/departure boards, delay estimates, cancellations | Live journey tracking while the user travels |
| Darwin Push Port | Real-time streamed updates for every service | Scale: track all monitored journeys server-side without polling |
| Historic Service Performance (HSP, JSON) | Actual arrival times up to a year back | Verify the delay after the fact and compute the exact compensation; also lets users back-claim recent journeys within the 28-day window |

Access is free via registration on the Rail Data Marketplace (up to 5M API requests per 4-week period; push feeds free). HSP is the workhorse: even if live tracking misses something, a nightly HSP sweep over every stored journey catches every qualifying delay.

## 5. Architecture (web + mobile app + backend)

**Web (Next.js) — the primary surface.** Server-rendered so it's indexable: per-operator Delay Repay guides, a free no-login "what am I owed?" calculator, the back-claim flow for the last 28 days, plus full account and claim management. Search is our main acquisition channel and an app can't be searched, so the web app is a distribution requirement, not a nice-to-have (see [MARKET §2a](MARKET.md)). It also serves the passengers least likely to install anything — older and less app-native users, who are over-represented among those losing out today.

**App (React Native, iOS + Android) — the notification loop.** Push notification within minutes of a delayed arrival, camera barcode scan, journey list, claim status, recovered-money total. Reliable push is the one thing the web genuinely can't match.

Both are thin clients of the same API; all logic stays server-side.

**Backend (Python, FastAPI, Postgres):**

- *Journey service* — stores tickets/journeys, matches them to scheduled services.
- *Delay engine* — consumes Darwin Push Port in real time, plus a nightly HSP reconciliation job; when a monitored journey qualifies, computes the entitlement and fires a push notification.
- *Claims service* — per-operator adapter pattern: each operator gets an adapter that either (a) generates a pre-filled form deep link, or (b) submits the form server-side (headless browser / HTTP form post). Start with adapter type (a) for everyone and type (b) for the biggest operators (e.g. Northern, Avanti, LNER, SWR, GTR brands). Store proof (ticket image, HSP record) with each claim.
- *Status tracker* — parses operator confirmation emails (user forwards them, or connects a mailbox later) to update claim state.

The adapter layer is the moat and the treadmill: operators change their forms, so build adapters with monitoring/alerts for breakage from day one.

## 6. Money flow and regulation

Do **not** route refunds into an in-app balance for the MVP. Holding customer money in the UK means FCA e-money/safeguarding obligations — months of compliance work before writing a line of product code. Instead the operator pays the user directly (their existing rails), and AutoTrain monetises as:

- **Free tier:** delay detection + pre-filled claim links.
- **Paid tier (~£2–3/mo or ~15–25% success fee):** fully automatic submission + claim tracking.

An in-app wallet can come later via a regulated partner (e.g. a BaaS provider) once volume justifies it. Note also that filing claims on a user's behalf may need explicit consent language in the T&Cs (letter-of-authority style), and some operator T&Cs restrict third-party claiming — worth a legal review before launching automated submission publicly.

## 7. Risks

The big three: **operator form changes** breaking adapters (mitigate: monitoring, fallback to pre-filled links); **operators blocking automated submissions** (mitigate: user-consent framing, human-speed submission, relationships with operators — some may welcome the reduced support load); and **fraudulent claims** through the app harming trust with operators (mitigate: require ticket proof, HSP verification before filing).

## 8. Build phases

1. **Weeks 1–2 — Delay engine proof.** Rail Data Marketplace access, HSP + LDB integration, CLI that takes a journey and answers "was it late, how much are you owed". No UI yet.
2. **Weeks 3–6 — Web MVP.** Next.js: the free "what am I owed?" calculator over the delay engine, back-claiming for the last 28 days, accounts, pre-filled claim links for every operator, and the first SEO content. Shipping this first starts the SEO clock — rankings take months to mature, so the content should exist well before we need the traffic — and it's cheaper to build and iterate than the app (no store review).
3. **Weeks 7–10 — App + notifications.** React Native: barcode scan, journey list, and the push notification loop on qualifying delays — the magic moment web can't deliver.
4. **Weeks 11–14 — Auto-filing.** Server-side claim submission for the top 3–5 operators; claim status tracking.
5. **Then:** email ticket ingestion (LLM extraction — see [ARCHITECTURE §6a](ARCHITECTURE.md)), more operator adapters, season tickets, paid tier.
6. **Maybe, v2:** a separate London-commuter feature for TfL contactless refunds, for users who want it — see §10. Not a change to the National Rail path.

## 9. Open questions

- Success fee vs subscription (success fee converts better but complicates money flow since the operator pays the user directly — you'd invoice the user after payout).
- Which operators to automate first — pick by a mix of passenger volume and worst punctuality (HSP data can rank this empirically).
- Whether to seek a relationship with Rail Delivery Group / operators early, or stay a pure consumer tool until traction.
- London (TfL) feature, if built: is the credential-free version (§10, level 0) good enough, or does the one-tap promise need the phone to sign into TfL (level 1)?

## 10. Maybe, v2: London commuters (TfL contactless refunds)

Written 2026-09-05 so it is not forgotten. Nothing here is committed to.

**The reference product.** [Reeclaim](https://www.reeclaim.co.uk/) sells exactly this, and only this: TfL-only, contactless bank cards, the user connects their TfL account, Reeclaim finds every eligible delay, pre-fills the refund, the user approves each one with a tap, Reeclaim submits, TfL credits the card in 5–10 working days. Subscription £4.99–£15.99 a month and no cut of the refund. Claimed scale (Sept 2026): 149K cards connected, 20M journeys, 375K claims, £1M+ refunded. It proves two things we care about: commuters pay a subscription for "nothing to do until a notification", and a subscription avoids the money-flow problems of a success fee (§6).

**Why it fits AutoTrain.** TfL is the single account holding every journey that National Rail does not have (§3, MARKET F1). The rules are simple: 15+ min on Underground/DLR, 30+ min on Overground/Elizabeth line, full fare back, claim within 28 days. TfL's public Unified API (free) gives line status and disruption data for detection. Contactless journeys on National Rail operators inside the London contactless area also appear in the TfL account, so this would cover part of our own market too.

**Shape.** A separate module, `tfl`, with its own tables and service, behind the same import contracts as the others (ARCHITECTURE §3). Four parts, each a shape the backend already has once: (1) a connection to the user's TfL account; (2) a journey importer — nightly sweep, stamp column, source behind a protocol, like the ingestor; (3) a delay detector — a pure function over TfL data, like the entitlement calculator but with TfL's rules; (4) a submitter — a `form_submit` adapter behind the consent gate (`users.claim_consent_at`). Reused as is: identity, notifications, the scheduler and worker loops, the money summary, and the one-tap approve screen.

**The credential problem, as a ladder.** Every level above 0 means acting inside the user's TfL account, and there is no official API for that.

0. **No credentials.** TfL accounts can email journey statements; if that can be set to automatic (weekly), the user forwards them to their AutoTrain address and they ride the same pipeline as ticket emails. Detection from the public API. Filing is a deep link to TfL's refund page, where the user signs in and submits themselves. Fully automatic detection, one extra tap to file, and we never see a password. *To verify: that automatic statement emails exist and their cadence fits the 28-day window.*
1. **Device-side.** The mobile app signs into TfL on the phone; the password stays in the phone's keychain; our server only receives journey data, and for filing the phone submits. This is Reeclaim's model ("encrypted on your device, never on our servers"). Browsers' cross-origin rules make it impossible from a web page, so it needs the native app.
2. **Server-side session.** Hold an encrypted TfL session token per user. Fully automatic and the worst risk. Only if 0 and 1 both fail the product.
3. **Official.** An API or data-sharing agreement with TfL. The only version with no grey area; needs TfL to agree.

**Before anything above level 0:** the legal read §6 and §7 already call for — TfL's terms, and automated access to an account with the user's permission. **Sequencing:** after the National Rail slice has real users. Everything here is a second copy of an existing piece, and second copies go faster once the first has been used.
