# AutoTrain — Market, Competitors & Pain Points

Feature-by-feature analysis of who else does this, where they fall short, what will hurt us, and how we overcome it. Companion to [PLAN.md](PLAN.md) and [ARCHITECTURE.md](ARCHITECTURE.md). *Researched 13 Aug 2026.*

## 0. The market in numbers

- Only **~37% of eligible passengers claim**; ~29% of passengers say they skipped compensation they were owed in 2025 ([ORR](https://www.orr.gov.uk/search-news/better-process-passengers-claim-delay-compensation-today), [Trainline research](https://www.trainlinegroup.com/media/news/delayed-rail-passengers-losing-out-on-more-than-80mn-each-year-in-unclaimed-compensation-due-to-delay-repay-frustration)).
- **£80m+/year goes unclaimed.** 58% say a claim takes 6+ minutes; 43% call the process frustrating.
- The blocker is exactly what PLAN.md assumed: not knowing you're eligible + claim-form friction. The market thesis is validated by independent data.

## 0a. Proof it works at n=1 — the origin tweet

AutoTrain's seed: [Owen Greenhalgh (@ogme01)](https://x.com/ogme01) tweeted (Aug 2026) — *"I have a codex scheduled task that checks my inbox for train tickets, works out which journeys arrived late and submits the delay repay claims… it's been cooking recently"* — with a screenshot of six Govia Thameslink Railway payments landing in his bank between 6–12 Aug 2026 (£1.30–£5.60 each, ~£17 total in a week).

**Reverse engineer:** 

1. **Scheduled agent** — a Codex task on a cron cadence; the whole loop runs unattended.
2. **Inbox ingestion** — the agent reads his email (Gmail API/IMAP access) and finds train ticket confirmations; the LLM extracts journey details directly from the raw email, no parser code.
3. **Delay check** — compares booked journeys against actual arrivals, almost certainly via the free HSP/Darwin data (or the operator's own delay-checker) — the same feeds in our Phase 1.
4. **Claim submission** — automates GTR's Delay Repay web form, filing from *his own operator account* with saved bank details — which is why the screenshot shows direct "Payment received" bank credits.

**What this single data point validates:**

- **The full loop is automatable today** — email → LLM extraction → delay verification → form submission → payout. No step required a partnership or API that doesn't exist. This is our architecture at n=1 (see [ARCHITECTURE §6a](ARCHITECTURE.md) — LLM ingestion at the edges, deterministic delay checks in the middle).
- **Filing as the user works.** GTR — the operator that pushed back on Delay Repay Sniper's third-party claims — paid six automated claims filed from the user's own account without friction. That's direct evidence for the file-as-the-user-with-consent model (§F4) over the claims-company model.
- **The unit economics warning in §2a, illustrated.** A committed commuter on one of Britain's least punctual operators recovered ~£17/week. Real money for him; at a 10% fee that's £1.70/week of revenue — subscription-shaped, not fee-shaped, for commuters.
- **The substitute-product threat is real but bounded.** Technical users can now self-serve this with an agent and a cron job — and as agent tools get easier, more will. AutoTrain's market is everyone who won't wire up inbox access and a scheduled agent themselves: the product is *packaging* this loop — trust, polish, zero setup, every operator — not the loop itself.

## 1. Competitive landscape

| Competitor | What they do | Coverage limit | Business model |
|---|---|---|---|
| **Operator auto-schemes** (LNER, Avanti, Southeastern "One Click", GWR, Thameslink smartcard) | Auto-detect + pre-filled claim, sometimes true auto-payout | **Only tickets bought direct from that operator**, usually Advance/smartcard only | Free (regulatory goodwill) |
| **Trainline** | Detects delay, push-notifies eligibility, links to the operator's form | **Only Trainline-bought tickets**; stops at the link — user still fills the claim | Drives retail loyalty |
| **Choo Choo** (National Rail–accredited retailer) | Delay Repay alerts + deep link to claim page; strong on price/split tickets | **Only its own bookings** get the smooth path; still not auto-submission | Ticket retail |
| **Delay Repay Sniper / Genie** | Detection + assisted claims, aimed at **season-ticket commuters** | Web-era product; users report breakage and operator pushback (GTR skepticism of its claims) | Subscription + "full service" tier |
| **Railed (gotrailed.co.uk)**  | **Our exact model**: forward ticket by email → they monitor → auto-file the claim → payout minus fee. iOS + Android. | Unknown operator coverage/depth | **10% success fee** |
| **TrainDelaysRepay.co.uk** | Claim-assistance app "against all UK TOCs" | Assisted, not automatic | Unclear |

**The key fact: [Railed](https://gotrailed.co.uk/) already built AutoTrain — and appears to have no traction.** Email-forward ingestion, automatic monitoring, automatic filing, 10% success fee, iOS + Android — and essentially no reviews or visible user base (checked Aug 2026). That's simultaneously good news (the universal-claim gap is still open; the concept is buildable; operators tolerate third-party auto-filing) and the loudest warning in this document: someone walked this exact path and stalled. The autopsy points at two structural problems, analysed in §2a — distribution and unit economics — and AutoTrain must answer both, not just out-build a company that may have died of neither engineering nor demand.

**Structural gap everyone shares:** the industry's own 1-click infrastructure is closed to third-party-retailer customers ([Trainline's complaint](https://www.trainlinegroup.com/media/news/delayed-rail-passengers-losing-out-on-more-than-80mn-each-year-in-unclaimed-compensation-due-to-delay-repay-frustration)) — retailers only serve their own tickets, operators only serve direct purchases. Nobody credibly serves *"any ticket, wherever you bought it"* — except Railed is attempting it. The race is to be the best universal layer.

## 2a. The Railed autopsy: distribution and unit economics

Why does a working product in an £80m/year market have no users? Two structural traps, both of which apply to us:

**Distribution.** Nobody searches for a delay-repay *app*, and app installs are a high-friction ask for a product used a handful of times a year — the need is felt *after* a delay and forgotten by morning. Trainline/Choo Choo sidestep this entirely: delay alerts are a retention feature inside an app people already open to buy tickets. Mitigations to design in from day one:

- **Search is the channel — which means the web app is not optional.** People don't search for the app, but they search the *problem* constantly: "train delayed compensation", "delay repay Thameslink", "how late does a train have to be to claim", "my train was cancelled can I get a refund". That is high-intent traffic arriving at peak motivation, and an app captures none of it. A server-rendered web surface with per-operator guides and a free "what am I owed?" calculator (no login) is the cheapest acquisition channel available, and it compounds — SEO takes months to mature, so the content should exist *before* it's needed. It also serves the users least likely to install anything: older and less app-native passengers, who are disproportionately the ones losing out today.
- **The screenshot loop needs a destination.** "AutoTrain just got me £12 for doing nothing" is inherently shareable; make the payout moment beautiful and one-tap shareable — and point the link at a web page that converts on the spot, not an app-store listing that leaks 80% of the click. Every delayed train is a trainful of identically-annoyed prospects; moment-of-delay marketing (search and social around major disruptions) hits them at peak motivation.
- **Back-claiming as the acquisition hook.** HSP lets a new user enter journeys from the last 28 days and instantly discover money they're already owed — value in the first session, before any waiting. This is a keyboard-and-screen task, which is another reason it belongs on web first.

**Unit economics of the success fee.** A typical 15–29 min claim pays 25% of a single fare — often £2–8, so a 10% fee earns *pennies*. Ten qualifying delays a year ≈ £5–10 revenue per user. This is why funded players give detection away: it's retention for ticket retail, not a business. Consequences for us (supersedes the pricing discussion in §F6 where they conflict):

1. **Segment matters more than rate.** 100% of a £180 long-distance Advance at 60+ min is a claim worth £18–45 in fee terms. Frequent long-distance travellers (and disruption-prone routes) are the profitable niche; hunt them deliberately rather than averaging across all rail users.
2. **Subscription beats success fee at the low end** (~£2–3/mo from a commuter beats 64p of fees) but drops us into Sniper's niche with its baggage; a hybrid (free detection, subscription for auto-filing, capped fee for big claims) is the likeliest shape. Decide with real payout-size data from the HSP-ranked operator analysis, not guesswork.
3. **Claims may be the wedge, not the endgame.** A product that puts money *into* users' pockets earns unusual trust and a payment relationship; ticket retail or broader travel-money products are the natural expansion if the standalone economics stay thin.

## 2. Feature-by-feature: pain points and how we overcome them

### F1 — Ticket capture (scan / manual / email-forward)

**Why it's the #1 battleground:** retailers (Trainline, Choo Choo) get tickets for free at purchase — we never will. Every second of capture friction costs conversion, and capture friction is the reason the 63% never claim in the first place.

| Pain point | Overcome by |
|---|---|
| Manual entry is exactly the friction we exist to remove | Barcode-first: UK e-tickets carry an RSP-standard Aztec barcode with structured journey/fare data — one scan, zero typing. Manual form is the fallback, not the default. |
| Email confirmations vary wildly by retailer | Email-forward ingestion (Railed's method) as fast-follow: start with parsers for the top 3 retailers (Trainline, Trainline-powered whitelabels, TOC direct), park unparseable emails for manual review, measure parser hit-rate as a KPI. |
| Season tickets / smartcards don't fit single-journey capture | Explicitly out of MVP (Sniper's niche); revisit once single-ticket flow wins. |
| Users forget to add tickets at all | Long-term: opt-in mailbox connection (OAuth Gmail scan) — the only true zero-effort capture a non-retailer can offer. Big privacy-trust cost; sequence it after trust is established. |

### F2 — Delay detection

**Reality check: detection is table stakes, not a moat.** Trainline and Choo Choo both do real-time detection and eligibility alerts well. We must match them, then win on what happens *after* detection.

| Pain point | Overcome by |
|---|---|
| Delay must be measured at the *user's destination*, not the terminus; operators reject claims over "wrong arrival time" | HSP-verified actuals per calling point before any claim is filed; never file on live estimates ([rejection reasons](https://gotrailed.co.uk/blog/why-delay-repay-claims-get-rejected/)). |
| Cancellations, re-routings, multi-leg journeys break naive matching | Segment-level model (ARCHITECTURE §4); missed-connection logic deferred but the schema supports it from day one. |
| Live feed gaps → missed delays → user checks manually once, deletes app | The nightly HSP sweep is the correctness guarantee: *every* qualifying delay is caught within 24h even if live tracking failed (ARCHITECTURE §5). None of the competitors advertises a reconciliation guarantee — "we never miss a delay" is a marketable claim. |

### F3 — Entitlement notification

| Pain point | Overcome by |
|---|---|
| A wrong "you're owed £6.40" destroys trust instantly (see Sniper's "£795 on a £265 season ticket" credibility problem) | Exact, HSP-verified, per-operator-rules amounts in integer pence; show the evidence (scheduled vs actual arrival) in the notification detail. Accuracy *is* the brand. |
| Operators' thresholds/scales differ (15 vs 30 min triggers) | Per-operator config table (ARCHITECTURE §4), covered by exhaustive unit tests. |
| Notification fatigue / arriving too late to feel magical | Notify within minutes of arrival (live path), with the amount and a one-tap action. The pre-arrival "trending late" prediction is a v2 wow-feature. |

### F4 — Claim filing (the moat)

**This is where every incumbent stops short or struggles.** Operators auto-file only for themselves; Trainline/Choo Choo stop at a link; Sniper shows the failure modes (breakage, operator friction); Railed is the one doing it and charges 10%.

| Pain point | Overcome by |
|---|---|
| No public claims API; every operator form is different and changes without notice | Adapter pattern + `adapter_health` monitoring + recorded-fixture contract tests so a form change fails CI, not production (ARCHITECTURE §6). Treat adapter reliability as the core engineering competency of the company. |
| Operators may block bots (CAPTCHA, rate-limits, WAF) | Human-speed submission, per-operator rate limiting, and always the pre-filled deep-link fallback so the user is never stranded. |
| Some operators refuse third-party claims outright (e.g. [EMR policy](https://www.eastmidlandsrailway.co.uk/help-manage/manage/make-a-delay-repay-claim)); GTR has pushed back on Sniper-originated claims | File **as the user, with the user's details and explicit letter-of-authority consent** — not as a claims company. Legal review of consent language before automated submission launches (PLAN §6). Where an operator still resists, degrade to deep-link and say so transparently. |
| Fraudulent/duplicate claims poison operator relationships | Ticket proof stored with every claim, HSP verification before filing, one-claim-per-segment idempotency (ARCHITECTURE §4). Being the *clean* third party is a long-term asset — operators talk to each other. |
| Which operators to automate first | Rank empirically: HSP delay volume × passenger volume × form automatability. Avoid spending adapter effort where operator auto-schemes already cover most direct buyers; prioritize operators with bad punctuality and no auto-scheme. |

### F5 — Claim status & payout tracking

**Weakest link in every competitor** — Sniper reviews complain about it, Railed's visibility is unknown, operators respond by unstructured email.

| Pain point | Overcome by |
|---|---|
| No status API; operators reply by email to the *user* (since we file as the user) | v1: user forwards operator emails; parse confirmations/rejections per operator template. Later: optional connected mailbox. Set expectations in-app ("operators typically pay in 5–20 days"). |
| Rejections are common (wrong operator, unclear evidence, amended timetables) and users give up | Every claim ships with HSP evidence attached; on rejection, auto-draft the appeal with the evidence pack. "We fight rejections" is a differentiator nobody offers. |
| Without visible outcomes the product feels like a black hole | The running "money recovered" total (PLAN §3) is the retention loop — make payout confirmation a celebration moment. |

### F6 — Pricing (vs Railed's 10%)

Our plan floated ~£2–3/mo or a 15–25% success fee. **Railed anchors the market at a 10% success fee.** Charging 15–25% for the same promise loses head-to-head.

- Success fee beats subscription for trust (pay only when you're paid) but means invoicing the user after the operator pays them — collection risk, and it's why Railed's model needs card-on-file.
- Options to test: match 10% with a superior product; free detection + flat fee per successful claim; or subscription cap ("never pay more than £3/mo"). Decide after using Railed and seeing where their experience disappoints.
- Keep the free tier (detection + deep links) — it's the acquisition engine and it out-features Trainline for non-Trainline tickets at zero cost.

## 3. Strategic summary

1. **Validated market, no entrenched incumbent — but a cautionary corpse.** £80m/year unclaimed and a 37% claim rate prove demand; Railed proves the concept is buildable but its lack of traction says engineering isn't the bottleneck. The hard problems are distribution and revenue-per-user (§2a), and they need answers as deliberate as the architecture.
2. **Positioning: the universal claim layer.** Retailers serve their tickets; operators serve their direct buyers; we serve *every* ticket. That's the structural gap the industry's own data admits exists — still unfilled.
3. **Trust is the product.** Sniper shows how assisted-claims services die: inflated numbers, breakage, operator hostility. Exact HSP-verified amounts, user-consent filing, attached evidence, transparent fallbacks.
4. **Economics before adapters.** Use HSP data early to size real payout distributions per route/operator — it tells us who the profitable users are, which operators to automate first, and whether subscription or fee pricing wins. It's the same Phase-1 integration the delay engine needs anyway.

### Sources

[ORR — claim process improvements](https://www.orr.gov.uk/search-news/better-process-passengers-claim-delay-compensation-today) · [Trainline Group — £80m unclaimed](https://www.trainlinegroup.com/media/news/delayed-rail-passengers-losing-out-on-more-than-80mn-each-year-in-unclaimed-compensation-due-to-delay-repay-frustration) · [ORR delay-compensation factsheet 2025-26](https://dataportal.orr.gov.uk/media/mzqfyttn/delay-compensation-claims-factsheet-2025-26-rail-periods-1-4.pdf) · [Trainline Delay Repay](https://www.thetrainline.com/trains/great-britain/delay-repay) · [Choo Choo](https://www.choochoo.co.uk/) · [Choo Choo — app comparison 2026](https://www.choochoo.co.uk/blog/what-is-each-uk-train-ticket-app-best-at-2026) · [Railed](https://gotrailed.co.uk/) · [Railed — why claims get rejected](https://gotrailed.co.uk/blog/why-delay-repay-claims-get-rejected/) · [Railed — automatic Delay Repay gaps](https://gotrailed.co.uk/blog/automatic-delay-repay-what-it-covers/) · [Delay Repay Sniper reviews](https://www.reviews.io/company-reviews/store/delayrepaysniper-com) · [RailUK forums — Sniper discussion](https://www.railforums.co.uk/threads/delay-compensation-delay-repay-sniper.115097/) · [RailUK forums — GTR vs Sniper claims](https://www.railforums.co.uk/threads/gtr-not-accepting-my-claim-through-delay-repay-sniper.163594/) · [Southeastern One Click Delay Repay](https://www.southeasternrailway.co.uk/help/refunds-and-compensation/delay-repay-compensation) · [Thameslink automatic Delay Repay](https://www.thameslinkrailway.com/help-and-support/delay-repay/auto-delay-repay) · [EMR Delay Repay](https://www.eastmidlandsrailway.co.uk/help-manage/manage/make-a-delay-repay-claim) · [TrainDelaysRepay](https://www.traindelaysrepay.co.uk/)
