-- Operators (TOCs) and their Delay Repay schemes.
--
-- Reference data, not user data. Seeded by a later migration; changed by
-- migration, not by application code — the set of UK operators changes a few
-- times a year at most, and every change should be reviewed like code.
--
-- Design notes:
--   * atoc_code is the natural key ('AW', 'GW', 'VT'...). We still use a
--     surrogate uuid PK so a franchise change (new code, same trains) does not
--     ripple through every referencing row.
--   * The sliding scale lives in delay_repay_bands, one row per band, rather
--     than columns like pct_15_29 — operators differ in band boundaries (some
--     start at 15 minutes, some at 30; some pay 100% of a RETURN at 120+), and
--     rows can express any scheme without schema changes.
--   * Percentages apply to the single fare unless of_return_fare is true.

CREATE TABLE operators (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    atoc_code      text        NOT NULL UNIQUE CHECK (atoc_code ~ '^[A-Z]{2}$'),
    name           text        NOT NULL,
    -- 'deep_link': we generate a pre-filled claim URL the user opens.
    -- 'form_submit': we submit the operator's web form server-side (Playwright).
    -- 'none': operator not yet supported; journeys still tracked, claims manual.
    adapter        text        NOT NULL DEFAULT 'none'
                               CHECK (adapter IN ('none', 'deep_link', 'form_submit')),
    claim_window_days integer  NOT NULL DEFAULT 28 CHECK (claim_window_days > 0),
    -- Minimum delay in minutes before ANY entitlement exists (15 or 30 today).
    min_delay_minutes integer  NOT NULL CHECK (min_delay_minutes > 0),
    claim_url      text,
    is_active      boolean     NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER operators_set_updated_at
    BEFORE UPDATE ON operators
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE operators IS
    'UK train operating companies and how we file Delay Repay against each.';
COMMENT ON COLUMN operators.atoc_code IS
    'Two-letter ATOC/TOC code as used by Darwin and HSP (e.g. GW, VT, NT).';
COMMENT ON COLUMN operators.adapter IS
    'Which claim-filing mechanism the claims module uses for this operator.';

CREATE TABLE delay_repay_bands (
    id              uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id     uuid    NOT NULL REFERENCES operators (id) ON DELETE CASCADE,
    -- Band is [min_minutes, max_minutes); NULL max = unbounded (the top band).
    min_minutes     integer NOT NULL CHECK (min_minutes >= 0),
    max_minutes     integer CHECK (max_minutes > min_minutes),
    percent         integer NOT NULL CHECK (percent BETWEEN 0 AND 100),
    of_return_fare  boolean NOT NULL DEFAULT false,

    UNIQUE (operator_id, min_minutes)
);

COMMENT ON TABLE delay_repay_bands IS
    'Per-operator Delay Repay sliding scale. Entitlement = the band whose '
    '[min_minutes, max_minutes) range contains the observed delay.';
COMMENT ON COLUMN delay_repay_bands.of_return_fare IS
    'True where the percentage applies to the price of a return ticket rather '
    'than the single fare (typically the 120+ band).';
