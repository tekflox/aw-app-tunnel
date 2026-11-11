"""A stand-in for core's ``ctx.state`` facade (aw-workspace
``src/apps/state_facade.py``), so this repo's tests can exercise the
leader/standby split without importing aw-workspace.

Three things it has to get right, because the production code depends on all
three and getting any of them wrong would make these tests prove nothing:

* **The lease is a real ``flock``.** ``fcntl.flock`` is held per *open file
  description*, not per process, so two ``FakeLease`` objects in one pytest
  process contend exactly like two uvicorn workers do — which is what lets a
  test have a leader and a standby at once. Non-blocking, one-shot,
  ``EAGAIN`` means someone else holds it: the same contract as
  ``fs_lock.try_acquire``, which the real facade wraps. No TTL and no renew —
  the held fd IS the liveness proof.
* **kv is shared and TTL'd.** One dict per "workspace", shared by every worker
  sharing a store, keyed the same way (one bare name per app). ``put`` rejects
  a missing/non-positive TTL, because the real facade does and a test that
  passed one would not catch an immortal-key regression.
* **broadcast is fan-out with no local loopback.** A publish reaches every
  *other* subscriber sharing the bus. The real one drops publishes silently
  when Redis is down, which is why no test here may use it as the mechanism
  under test — only as a latency optimisation on top of the tick.
"""
from __future__ import annotations

import fcntl
import json
import os
import time


class FakeKv:
    def __init__(self, shared: dict) -> None:
        self._shared = shared

    async def put(self, name: str, value, ttl: float) -> None:
        if not ttl or ttl <= 0:
            raise ValueError("ttl is required and must be > 0")
        self._shared[name] = (time.time() + ttl, json.dumps(value))

    async def get(self, name: str):
        entry = self._shared.get(name)
        if entry is None:
            return None
        expires_at, raw = entry
        if time.time() >= expires_at:
            self._shared.pop(name, None)
            return None
        return json.loads(raw)

    async def delete(self, name: str) -> None:
        self._shared.pop(name, None)


class FakeBroadcastBus:
    """The Redis pub/sub every worker in one test shares."""

    def __init__(self) -> None:
        self.subscribers: list[tuple[object, str, object]] = []
        self.published: list[tuple[str, dict]] = []
        self.down = False


class FakeBroadcast:
    def __init__(self, bus: FakeBroadcastBus, owner: object) -> None:
        self._bus = bus
        self._owner = owner
        self.closed = False

    async def subscribe(self, topic: str, handler) -> None:
        self._bus.subscribers.append((self._owner, topic, handler))

    async def publish(self, topic: str, payload: dict) -> None:
        self._bus.published.append((topic, payload))
        if self._bus.down:
            return  # dropped, exactly like the real one with Redis unreachable
        for owner, sub_topic, handler in list(self._bus.subscribers):
            if owner is not self._owner and sub_topic == topic:
                await handler(payload)

    async def aclose(self) -> None:
        self.closed = True


class FakeLease:
    """Flock-backed, one non-blocking try, no TTL — see the module docstring."""

    def __init__(self, lock_dir: str, app_id: str = "tunnel") -> None:
        self._dir = os.path.join(lock_dir, "apps", app_id)
        os.makedirs(self._dir, exist_ok=True)
        self._held: dict[str, int] = {}

    def _path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.lock")

    def claim(self, name: str) -> bool:
        if name in self._held:
            return True
        fd = os.open(self._path(name), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._held[name] = fd
        return True

    def release(self, name: str) -> None:
        fd = self._held.pop(name, None)
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def release_all(self) -> None:
        for name in list(self._held):
            self.release(name)


class FakeState:
    """``ctx.state``: kv + broadcast shared across the workers of one test,
    lease genuinely contended through the filesystem."""

    def __init__(self, lock_dir: str, kv_shared: dict,
                 bus: FakeBroadcastBus) -> None:
        self.kv = FakeKv(kv_shared)
        self.broadcast = FakeBroadcast(bus, owner=self)
        self.lease = FakeLease(lock_dir)
