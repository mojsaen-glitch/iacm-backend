-- Crew Assignment Acceptance: supervisory admin-confirm fields.
-- acknowledged/acknowledged_at + declined/declined_at/decline_reason already
-- exist (create_tables.sql + 2026_05_17_assignment_decline.sql). Old rows keep
-- working: defaults make them 'pending_acceptance' (accept or admin-confirm
-- before the next finalize).
-- Run in the Supabase SQL editor.

alter table assignments
  add column if not exists admin_confirmed boolean default false,
  add column if not exists admin_confirmed_by text,
  add column if not exists admin_confirmed_at timestamptz,
  add column if not exists admin_confirm_reason text;
