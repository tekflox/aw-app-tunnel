-- 0002 — "stopped on purpose", as a durable fact instead of one worker's memory.
--
-- Unqualified on purpose, same as 0001: src/apps/migrations.py sets search_path
-- to this workspace's schema for the transaction.
--
-- Core runs several uvicorn workers and a Tier-1 app loads into every one of
-- them, so the old in-memory stop (forwarder.TunnelManager.stop) only ever
-- reached the single worker that happened to serve the HTTP request. With the
-- listener now pinned to one leader worker, "stop without disabling" needs a
-- channel every worker can read: broadcast drops silently when Redis is down,
-- and the reconcile tick reads the store anyway, so the store is that channel.
--
-- Deliberate semantic change: a manually stopped tunnel now STAYS stopped
-- across a core restart. Predictable beats re-deriving boot-vs-takeover.

ALTER TABLE "app__tunnel__tunnels"
    ADD COLUMN IF NOT EXISTS manual_stopped BOOLEAN NOT NULL DEFAULT FALSE;
