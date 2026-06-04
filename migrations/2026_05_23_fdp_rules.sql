-- ════════════════════════════════════════════════════════════════
--  FDP (Flight Duty Period) limit tables — EASA ORO.FTL-style
--
--  max_fdp_minutes is the maximum allowed Flight Duty Period for a duty
--  that STARTS within [start_band_from, start_band_to] (local Baghdad time)
--  and operates [sectors_from … sectors_to] sectors, for a given
--  acclimatisation state. The Compliance Engine reads this table, so limits
--  can be tuned per authority WITHOUT code changes.
--
--  States: 'acclimated' | 'not_acclimated' | 'unknown' (FRM/conservative)
--  Sector buckets: 1-2, 3, 4, 5, 6, 7, 8+ (8 stored as 8..99)
--  Bands are stored non-wrapping (a night band is split at midnight).
--  Safe to run multiple times — seeds only when the table is empty.
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS fdp_rules (
    id                     TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    acclimatisation_state  TEXT NOT NULL,
    start_band_from        TIME NOT NULL,
    start_band_to          TIME NOT NULL,
    sectors_from           INT  NOT NULL,
    sectors_to             INT  NOT NULL,
    max_fdp_minutes        INT  NOT NULL,
    is_frm                 BOOLEAN DEFAULT FALSE,
    created_at             TIMESTAMPTZ DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM fdp_rules) THEN
    INSERT INTO fdp_rules (acclimatisation_state, start_band_from, start_band_to, sectors_from, sectors_to, max_fdp_minutes, is_frm) VALUES
    -- ── Acclimated — morning/day start 05:00–13:29 ──
    ('acclimated','05:00','13:29',1,2,780,false),
    ('acclimated','05:00','13:29',3,3,750,false),
    ('acclimated','05:00','13:29',4,4,720,false),
    ('acclimated','05:00','13:29',5,5,690,false),
    ('acclimated','05:00','13:29',6,6,660,false),
    ('acclimated','05:00','13:29',7,7,630,false),
    ('acclimated','05:00','13:29',8,99,600,false),
    -- ── Acclimated — afternoon 13:30–16:59 ──
    ('acclimated','13:30','16:59',1,2,720,false),
    ('acclimated','13:30','16:59',3,3,690,false),
    ('acclimated','13:30','16:59',4,4,660,false),
    ('acclimated','13:30','16:59',5,5,630,false),
    ('acclimated','13:30','16:59',6,6,600,false),
    ('acclimated','13:30','16:59',7,7,570,false),
    ('acclimated','13:30','16:59',8,99,540,false),
    -- ── Acclimated — evening 17:00–23:59 ──
    ('acclimated','17:00','23:59',1,2,660,false),
    ('acclimated','17:00','23:59',3,3,630,false),
    ('acclimated','17:00','23:59',4,4,600,false),
    ('acclimated','17:00','23:59',5,5,570,false),
    ('acclimated','17:00','23:59',6,99,540,false),
    -- ── Acclimated — early hours 00:00–04:59 (WOCL — most restrictive) ──
    ('acclimated','00:00','04:59',1,2,660,false),
    ('acclimated','00:00','04:59',3,3,630,false),
    ('acclimated','00:00','04:59',4,4,600,false),
    ('acclimated','00:00','04:59',5,99,540,false),
    -- ── Not acclimated (any start) ──
    ('not_acclimated','00:00','23:59',1,2,660,false),
    ('not_acclimated','00:00','23:59',3,3,630,false),
    ('not_acclimated','00:00','23:59',4,4,600,false),
    ('not_acclimated','00:00','23:59',5,5,570,false),
    ('not_acclimated','00:00','23:59',6,99,540,false),
    -- ── Unknown state of acclimatisation (FRM conservative, any start) ──
    ('unknown','00:00','23:59',1,2,720,true),
    ('unknown','00:00','23:59',3,3,690,true),
    ('unknown','00:00','23:59',4,4,660,true),
    ('unknown','00:00','23:59',5,5,630,true),
    ('unknown','00:00','23:59',6,6,600,true),
    ('unknown','00:00','23:59',7,7,570,true),
    ('unknown','00:00','23:59',8,99,540,true);
  END IF;
END $$;
