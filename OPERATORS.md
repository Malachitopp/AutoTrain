# Operator filing survey

Can AutoTrain file a Delay Repay claim for a user, and for whom? Researched
2026-09-07 by three agents reading operators' own charters, help pages and
shipped JavaScript bundles. Every "verified" claim below came from fetching the
thing, not from a summary of it. Nothing here was tested against a live
account: no claim was submitted, nothing authenticated.

## There is no national rule

The National Rail Conditions of Travel was read in full (1,776 lines).
Conditions 32 and 33 set the deadline (28 days), the evidence (a ticket or
other authority to travel) and the entitlement, and say **nothing** about who
may submit a claim. A third-party restriction can only arrive through
Condition 32.1.1.2 — the operator's own Passenger's Charter. So this question
is per-operator and cannot be answered once.

## Nine operators, three platforms

The single most useful finding: the nine supported operators run only three
distinct claim systems, so three adapters cover all of them.

| Platform | Operators | How it was established |
| --- | --- | --- |
| **smartREPAY** (Angular SPA) | GWR, Avanti, Northern, South Western | GWR's and Avanti's sites return byte-identical HTML with the same `Last-Modified`, the same content-hashed asset filenames (`main.e168ee443dc7ba94.js`), the same reCAPTCHA site key and the same Sentry DSN. Only `/en/config.*.js` differs, carrying `toc: {id, atocCode}` — Northern is toc 30/NT, SWR toc 31/SW. |
| **OTRL** (Next.js, `api.otrl.io`) | Great Northern, Southern, Thameslink, Southeastern (+ Gatwick Express) | The three GTR sites differ only in brand slug, manifest path and favicon. All serve `/otrl-config.js` with the same `apiHost` **and the same `apiToken`**. Each hardcodes the same five-brand list. |
| **Bespoke** (ASP.NET WebForms) | LNER | Own version string (1.7.0.7), `__VIEWSTATE` fields, shares nothing. Note LNER's own sister brand Lumo is on the *other* platform — common ownership does not predict a shared adapter. |

## Who permits a third party to file

| Operator | Third party | Account needed | Auto-claims already |
| --- | --- | --- | --- |
| **GWR** | **Yes, explicit** | Yes | Direct bookings, opt-in, still confirmed by hand |
| **Southeastern** | **Yes** | No — guest claims | Partial |
| **South Western** | **Yes, in writing** | Yes | Yes |
| LNER | Silent | No — guest claims | Opt-in, direct bookings only |
| Avanti | Silent | Yes | Partial |
| Northern | Silent | Yes | Partial |
| Great Northern / Southern / Thameslink | **No, explicit** | No | Partial |

GWR's Passenger's Charter, p.36, is the clearest wording found anywhere:

> "We accept claims made by a third party as long as they include the
> passenger name and journey information."

Govia Thameslink's is the clearest refusal:

> "We cannot accept claims from a third party unless there are mitigating
> circumstances you tell us about."

"Silent" means verified absence — the charters were read and say nothing
either way. That is not permission.

Every operator's own automatic scheme covers only tickets bought **direct
from them**, requires opting in, and still needs the passenger to confirm.
None of them cover a ticket bought through a retailer, a split ticket, or
another operator's service. That gap is the product.

## reCAPTCHA is on appeals, not on claims (smartREPAY)

Checked directly in the shipped 6 MB bundles for GWR and for Northern — two
independent releases:

- exactly **one** `reCaptchaSiteKey` reference and **one** `siteKey` binding
  in the whole application, in both bundles;
- that binding sits in the component carrying `appealReason`, `claimUrn`,
  `awardAmount` and `isAppealClaim()` — the **appeal** form;
- `recaptcha/api.js` is loaded with no `?render=` parameter, so it is v2, not
  v3 scoring;
- no `size: "invisible"` is set anywhere.

So on smartREPAY the claim submission itself is not captcha-gated; appealing a
rejected claim is. This has NOT been confirmed against the server, which could
reject non-browser traffic by other means, and it has not been checked at all
for OTRL — the other agent reported reCAPTCHA on that platform.

## Every operator requires a ticket image

Without exception. This section said the blocker was that AutoTrain captured
no attachments. That half is now false — migration 0020 stores them and the
IMAP mailbox source delivers them — but the blocker did not clear. It moved,
and it moved somewhere worse: to the retailer.

**Trainline does not email the ticket.** Verified 2026-09-07 against a real
confirmation (Gatwick Airport to St Pancras, 23 August, £9.65), read straight
off the mailbox. The whole 94 KB message is two parts:

| Part | Disposition | Size |
| --- | --- | --- |
| `text/html` | inline | 81,591 bytes |
| `text/calendar` (`trip.ics`) | attachment | 998 bytes |

A calendar invite, and nothing else. No PDF, no image, no barcode. For a
Trainline booking the ticket is collected from a station machine or lives in
their app, so it never touches the inbox at all — and no amount of attachment
capture can find something that was never sent.

So "can we file?" now splits by **who sold the ticket**, not by who ran the
train:

* **Bought direct from an operator** — the e-ticket PDF is usually attached,
  and 0020 now keeps it. This is the case that works.
* **Bought through Trainline** (and probably the other third-party
  retailers) — there is nothing in the email to keep. Filing needs the ticket
  from somewhere else: the retailer's own account, an app export, or the
  passenger photographing it.

That second case is the one the product was originally for — the gap where an
operator's own automatic scheme does not reach. Worth knowing that it is also
the harder half to file.

Still unverified: whether a direct operator booking really does attach a PDF.
*Book one cheap advance ticket direct from LNER or GWR and look at the parts
of the confirmation.* Until that is done, attachment capture is built and
proven to run, but has never once had a real ticket to store.

## Five UK services already do this

Three models, and the middle one is AutoTrain's:

- **Detect and hand off.** Trainline and DelayRepay.uk notify you and link to
  the operator's form. No third-party exposure at all.
- **File on the passenger's behalf for a commission.** [Railed](https://gotrailed.co.uk/)
  — "We automatically claim UK train delay refunds for you… minus 10% for our
  efforts. Just email us your tickets." Their terms (§3, Nov 2025) say they
  "file Delay Repay claims with the relevant train operator on your behalf".
  Ticket intake by email forwarding, the same as ours.
- Others found and not yet studied: MyRailBuddy, RefundMyRail,
  TrainDelaysRepay.

## Known expiry date

RDG (Rail Settlement Plan Ltd) is procuring a **single national Delay Repay
platform** — Find a Tender notice 053093-2026, published 4 June 2026,
£21,194,340, aligned to Great British Railways. Per-operator adapters have a
visible shelf life, and that migration is also the moment the third-party rule
could become uniform, in either direction.

## What is still unknown, and what settles it

1. **The portals' own terms of use.** Charter permission to file a claim is
   not permission to script a website, and those terms appear only at
   registration. *Register one account on smartREPAY and one on OTRL and read
   what you accept.* This is the most important open question.
2. **Whether OTRL's claim form is captcha-gated**, as smartREPAY's is not.
   *Walk one claim with devtools open.*
3. **Third-party permission for LNER, Avanti and Northern.** All silent.
   *A written question to customer relations is the only definitive route,
   and worth having on file regardless.*
4. **A possible sanctioned route.** The smartREPAY bundle contains a
   `thirdPartyToken` parameter, a `thirdPartyClaim` wizard mode and a
   `LANDING_LOGIN_BLURB_THIRD_PARTY` content slot — this looks like an
   official partner integration, which would beat scripting anything.
   *Ask GWR or the platform vendor who issues those tokens.*
5. **The claim submission's real field list and any "I confirm I am the
   passenger" declaration.** That declaration is exactly where a filing tool
   is permitted or caught out. *Only visible by completing one claim.*
