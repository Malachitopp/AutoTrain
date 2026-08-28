-- Turn on v1 claim filing (PLAN §3 item 4: opens the operator's Delay Repay
-- form, "pre-filled wherever possible") for the launch operators: adapter
-- becomes 'deep_link' and claim_url names the operator's claims portal.
--
-- Every URL below was checked against the operator's own site on 2026-08-28 —
-- the 0008 rule: verify against the operator page before enabling an adapter
-- beyond 'none'. Each is the operator's claims portal on its own
-- delayrepay.<domain> subdomain; LNER's carries its /delayrepayV2/ path
-- because that is what lner.co.uk links and the bare domain was not verified
-- to redirect.
--
-- claim_url is the CLAIM PAGE, not a prefill template: operator forms do not
-- take querystring parameters, so "pre-filled wherever possible" starts as
-- "the right page". If an operator's form turns out to accept parameters,
-- that lands in its adapter (modules/claims/adapters.py), not in this column.

UPDATE operators SET adapter = 'deep_link', claim_url = v.url
FROM (VALUES
    ('NT', 'https://delayrepay.northernrailway.co.uk/'),
    ('VT', 'https://delayrepay.avantiwestcoast.co.uk/'),
    ('GR', 'https://delayrepay.lner.co.uk/delayrepayV2/'),
    ('SW', 'https://delayrepay.southwesternrailway.com/'),
    ('TL', 'https://delayrepay.thameslinkrailway.com/'),
    ('GN', 'https://delayrepay.greatnorthernrail.com/'),
    ('SN', 'https://delayrepay.southernrailway.com/'),
    ('SE', 'https://delayrepay.southeasternrailway.co.uk/'),
    ('GW', 'https://delayrepay.gwr.com/')
) AS v(atoc_code, url)
WHERE operators.atoc_code = v.atoc_code;

-- An adapter with nowhere to send the user is a misconfiguration, made
-- unrepresentable: deep_link IS the URL handoff, and form_submit's failure
-- policy is the deep-link fallback (ARCHITECTURE §6), so both need a URL.
ALTER TABLE operators ADD CONSTRAINT operators_adapter_has_claim_url
    CHECK (adapter = 'none' OR claim_url IS NOT NULL);
