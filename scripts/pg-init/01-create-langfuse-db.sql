-- Create the vendor-managed Langfuse database alongside the app's "yuyutsava"
-- database in the SAME Postgres server. The two share a cluster but never a
-- schema — Langfuse owns and migrates everything inside its own database.
--
-- This file runs ONCE, only on a fresh data volume (the Postgres image's
-- /docker-entrypoint-initdb.d hook). For an already-initialized cluster the
-- `pg-ensure-langfuse-db` one-shot service in docker-compose.yml creates the
-- database idempotently instead.
--
-- CREATE DATABASE cannot run inside a transaction block, so generate the
-- statement and run it via \gexec (guarded so a re-run is a no-op).
SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
