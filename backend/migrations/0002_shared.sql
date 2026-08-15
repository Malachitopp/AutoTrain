-- Shared helpers used by every later migration.
--
-- Kept in its own file so the domain migrations can attach the trigger without
-- worrying about definition order.

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION set_updated_at() IS
    'BEFORE UPDATE trigger function: stamps updated_at. Attached per table — Postgres '
    'triggers are not inherited, so every table with updated_at needs its own.';
