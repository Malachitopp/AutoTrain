"""Per-operator claim-filing adapters — the deep-link half of ARCHITECTURE §6.

Filing goes through an adapter, registry-loaded from the operator's `adapter`
column, so adding or fixing an operator touches nothing outside its adapter.
§6 names two kinds: 'deep_link' (v1 — hand the user the operator's claim form
and let them file it) and 'form_submit' (v2 — drive the form server-side with
Playwright). Only v1 exists. form_submit operators will still need this path
when v2 lands: §6's policy after N submission failures is the deep-link
fallback — degraded, never dropped.

No adapter_runs rows are written here: 0007 defines those as one row per
claim-SUBMISSION attempt, and a deep-link handoff never touches the
operator's site — there is nothing to monitor for breakage.

Claims-internal: nothing outside the module may import this (the
claims-privacy contract) — filing is reached through service.file_claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from autotrain.modules.claims.models import ClaimRow, OperatorFiling


@dataclass(frozen=True)
class DeepLink:
    """What v1 filing produces: where this user files this claim, by hand."""

    url: str


class DeepLinkAdapter:
    """The generic v1 adapter: the operator's claims portal, as stored.

    Operator forms do not take querystring prefill, so PLAN §3's "pre-filled
    wherever possible" starts as "the right page". An operator whose form
    turns out to accept parameters gets a subclass building them from the
    claim — registered here, touching nothing else (§6).
    """

    def deep_link(self, operator: OperatorFiling, claim: ClaimRow) -> DeepLink:
        if operator.claim_url is None:
            # Unreachable: for_operator hands this adapter out only when a URL
            # exists, and 0011's CHECK keeps deep_link rows from losing theirs.
            raise RuntimeError("deep_link adapter selected for an operator with no claim_url")
        return DeepLink(url=operator.claim_url)


_REGISTRY: dict[str, DeepLinkAdapter] = {"deep_link": DeepLinkAdapter()}


def for_operator(operator: OperatorFiling) -> DeepLinkAdapter | None:
    """The adapter that can produce a filing link for this operator, or None.

    None for 'none' (0008 seeds every operator there until its portal URL is
    verified), for 'form_submit' (v2, unbuilt), and for an inactive operator:
    a franchise change kills the portal, so a stale link is never handed out.
    """
    if not operator.is_active or operator.claim_url is None:
        return None
    return _REGISTRY.get(operator.adapter)
