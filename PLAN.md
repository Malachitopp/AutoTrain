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

## 5. Architecture (mobile app + backend)

**App (React Native, iOS + Android):** onboarding, ticket capture (camera barcode scan + manual form), journey list, delay notifications (push), claim status screen, recovered-money total. Keep the app thin — all logic server-side.

**Backend (Node or Python, Postgres):**

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

1. **Weeks 1–2 — Delay engine proof.** Rail Data Marketplace access, HSP + LDB integration, CLI that takes a journey and answers "was it late, how much are you owed". No app yet.
2. **Weeks 3–6 — App MVP.** React Native app: manual journey entry, push notification on qualifying delay, pre-filled claim link for every operator. This alone is shippable and useful.
3. **Weeks 7–10 — Auto-filing.** Barcode ticket scan; server-side claim submission for the top 3–5 operators; claim status tracking.
4. **Then:** email ticket ingestion, more operator adapters, season tickets, paid tier.

## 9. Open questions

- Success fee vs subscription (success fee converts better but complicates money flow since the operator pays the user directly — you'd invoice the user after payout).
- Which operators to automate first — pick by a mix of passenger volume and worst punctuality (HSP data can rank this empirically).
- Whether to seek a relationship with Rail Delivery Group / operators early, or stay a pure consumer tool until traction.
