-- Seed: the operators we launch monitoring with, and their Delay Repay scales.
--
-- Reference data as a migration (not runtime inserts) so every environment gets
-- the same operators, reviewed like code. Percentages/thresholds from each
-- operator's published Delay Repay scheme; verify against the operator page
-- before enabling an adapter beyond 'none'.
--
-- The standard national scheme (Delay Repay 15):
--   15-29 min: 25% of single | 30-59: 50% of single
--   60-119: 100% of single   | 120+: 100% of return
-- Operators on Delay Repay 30 have no 15-29 band.

INSERT INTO operators (atoc_code, name, min_delay_minutes, adapter) VALUES
    ('NT', 'Northern',                    15, 'none'),
    ('VT', 'Avanti West Coast',           15, 'none'),
    ('GR', 'LNER',                        30, 'none'),
    ('SW', 'South Western Railway',       15, 'none'),
    ('TL', 'Thameslink',                  15, 'none'),
    ('GN', 'Great Northern',              15, 'none'),
    ('SN', 'Southern',                    15, 'none'),
    ('SE', 'Southeastern',                15, 'none'),
    ('GW', 'Great Western Railway',       15, 'none'),
    ('LE', 'Greater Anglia',              15, 'none'),
    ('EM', 'East Midlands Railway',       15, 'none'),
    ('XC', 'CrossCountry',                30, 'none'),
    ('AW', 'Transport for Wales',         15, 'none'),
    ('CH', 'Chiltern Railways',           15, 'none'),
    ('ME', 'Merseyrail',                  15, 'none'),
    ('SR', 'ScotRail',                    30, 'none'),
    ('TP', 'TransPennine Express',        15, 'none'),
    ('WM', 'West Midlands Trains',        15, 'none'),
    ('HX', 'Heathrow Express',            15, 'none'),
    ('GC', 'Grand Central',               30, 'none'),
    ('HT', 'Hull Trains',                 30, 'none'),
    ('LD', 'Lumo',                        30, 'none'),
    ('CC', 'c2c',                         15, 'none');

-- Delay Repay 15 bands for operators whose min_delay_minutes = 15.
INSERT INTO delay_repay_bands (operator_id, min_minutes, max_minutes, percent, of_return_fare)
SELECT o.id, b.min_minutes, b.max_minutes, b.percent, b.of_return_fare
FROM operators o
CROSS JOIN (VALUES
    (15,  30,   25, false),
    (30,  60,   50, false),
    (60,  120, 100, false),
    (120, NULL, 100, true)
) AS b(min_minutes, max_minutes, percent, of_return_fare)
WHERE o.min_delay_minutes = 15;

-- Delay Repay 30 bands for the rest.
INSERT INTO delay_repay_bands (operator_id, min_minutes, max_minutes, percent, of_return_fare)
SELECT o.id, b.min_minutes, b.max_minutes, b.percent, b.of_return_fare
FROM operators o
CROSS JOIN (VALUES
    (30,  60,   50, false),
    (60,  120, 100, false),
    (120, NULL, 100, true)
) AS b(min_minutes, max_minutes, percent, of_return_fare)
WHERE o.min_delay_minutes = 30;
