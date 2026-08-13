-- Extensions the domain schema assumes exist. Kept separate from the schema
-- itself so 0002 can use citext in a column type without ordering worries.
--
-- Not here, deliberately:
--   * gen_random_uuid() is core since Postgres 13 — no extension needed.
--   * pg_stat_statements needs shared_preload_libraries, so it is enabled by the
--     RDS parameter group in Terraform, not by a migration (ARCHITECTURE §7, §8).

CREATE EXTENSION IF NOT EXISTS citext;
