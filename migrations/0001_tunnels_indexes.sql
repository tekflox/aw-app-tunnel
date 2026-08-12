-- 0001 — uniqueness for the tunnels table.
--
-- Unqualified on purpose: src/apps/migrations.py sets search_path to this
-- workspace's schema for the transaction, so the same file is correct in every
-- tenant. Do not hard-code a schema name here.
--
-- store.py already rejects a duplicate port/name at validation time, but that
-- check reads-then-writes with no lock: two concurrent creates can both pass it
-- and only the DB can actually settle the race. A listener collision surfaces
-- much later as an unrelated "address in use", so pin it here too.

CREATE UNIQUE INDEX IF NOT EXISTS "app__tunnel__tunnels_listen_port_key"
    ON "app__tunnel__tunnels" (listen_port);

CREATE UNIQUE INDEX IF NOT EXISTS "app__tunnel__tunnels_name_lower_key"
    ON "app__tunnel__tunnels" (lower(name));
