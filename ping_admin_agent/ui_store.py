"""Front-end state shared by the worker processes of one runtime instance.

Agent Runtime serves an instance from several worker processes and routes each
request to any of them, so a click can land on a different worker than the
search that produced it. Views, action tokens, pending approvals, the
conversation lease and the SDK's A2A tasks therefore live in a SQLite file on
the instance's local disk, which every worker shares. The file lives and dies
with the instance: a restart or scale-to-zero discards open views and
approvals, and the operator opens a fresh workspace. The in-memory store serves
single-process local development and tests. Transactions never execute tools.
"""

import asyncio
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager, closing
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from a2a.server.tasks import TaskStore
from a2a.types import Task
from a2a.utils.errors import UnsupportedOperationError
from google.protobuf.json_format import MessageToDict, ParseDict

from .ui_actions import UiActionError

SURFACE_TTL = 1800
CONFIRMATION_TTL = 600
SESSION_TTL = 30 * 86400
TASK_TTL = 86400
LOCK_TTL = 600
MAX_RECORD_BYTES = 4_000_000


class MemoryStore:
    """Single-process state for local development and tests."""

    def __init__(self):
        self.records = {}
        self.lock = asyncio.Lock()

    async def get(self, kind, key):
        async with self.lock:
            value, expiry = self.records.get((kind, key), (None, 0))
            return deepcopy(value) if expiry > time.time() else None

    async def mutate(self, kind, key, update, ttl=SURFACE_TTL):
        async with self.lock:
            now = time.time()
            self.records = {k: v for k, v in self.records.items() if v[1] > now}
            if len(self.records) >= 4096 and (kind, key) not in self.records:
                raise UiActionError(
                    "The agent is handling too many open views. Try again later."
                )
            value, expiry = self.records.get((kind, key), (None, 0))
            result = update(deepcopy(value) if expiry > now else None)
            if result is None:
                self.records.pop((kind, key), None)
            else:
                self.records[(kind, key)] = (deepcopy(result), now + ttl)
            return deepcopy(result)

    async def put(self, kind, key, value, ttl=SURFACE_TTL):
        return await self.mutate(kind, key, lambda _: value, ttl)


class SqliteStore:
    """State shared by every worker process of an instance through one file.

    ``mutate`` runs its update inside an immediate transaction, so workers
    serialize on the record and a single-use token is consumed exactly once.
    Expired rows are ignored on read and pruned on write.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, "
                "expires_at REAL NOT NULL, PRIMARY KEY (kind, key))"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=15, isolation_level=None)

    @staticmethod
    def _read(connection, kind, key):
        row = connection.execute(
            "SELECT payload, expires_at FROM records WHERE kind = ? AND key = ?",
            (kind, key),
        ).fetchone()
        if not row or row[1] <= time.time():
            return None
        return json.loads(row[0])

    async def get(self, kind, key):
        def read():
            with closing(self._connect()) as connection:
                return self._read(connection, kind, key)

        return await asyncio.to_thread(read)

    async def mutate(self, kind, key, update, ttl=SURFACE_TTL):
        def transact():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    now = time.time()
                    connection.execute(
                        "DELETE FROM records WHERE expires_at <= ?", (now,)
                    )
                    result = update(self._read(connection, kind, key))
                    if result is None:
                        connection.execute(
                            "DELETE FROM records WHERE kind = ? AND key = ?",
                            (kind, key),
                        )
                    else:
                        payload = json.dumps(result, separators=(",", ":"))
                        if len(payload.encode()) > MAX_RECORD_BYTES:
                            raise ValueError("State record exceeds the supported size")
                        connection.execute(
                            "INSERT OR REPLACE INTO records "
                            "(kind, key, payload, expires_at) VALUES (?, ?, ?, ?)",
                            (kind, key, payload, now + ttl),
                        )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                return deepcopy(result)

        return await asyncio.to_thread(transact)

    async def put(self, kind, key, value, ttl=SURFACE_TTL):
        return await self.mutate(kind, key, lambda _: value, ttl)


def state_mode() -> str:
    managed = bool(os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID"))
    mode = os.getenv("PING_ADMIN_STATE_STORE", "").strip().lower() or (
        "sqlite" if managed else "memory"
    )
    if mode not in {"memory", "sqlite"}:
        raise ValueError("PING_ADMIN_STATE_STORE must be sqlite or memory")
    if managed and mode == "memory":
        raise ValueError(
            "Managed deployments require PING_ADMIN_STATE_STORE=sqlite: an instance "
            "serves requests from several worker processes that must share state"
        )
    return mode


def state_path() -> str:
    directory = os.getenv("PING_ADMIN_STATE_DIR", "").strip() or os.path.join(
        tempfile.gettempdir(), "pingaic-state"
    )
    namespace = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "").strip() or "local"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", namespace):
        raise ValueError("Unsupported state namespace")
    return os.path.join(directory, f"{namespace}.sqlite3")


def build_store():
    if state_mode() == "memory":
        return MemoryStore()
    return _shared_store(state_path())


@lru_cache(maxsize=4)
def _shared_store(path):
    return SqliteStore(path)


@asynccontextmanager
async def conversation_lock(store, key):
    owner = secrets.token_urlsafe(24)

    def acquire(record):
        if record:
            raise UiActionError(
                "A request is already running in this conversation. Wait for it to finish."
            )
        return {"owner": owner}

    await store.mutate("lock", key, acquire, ttl=LOCK_TTL)
    try:
        async with asyncio.timeout(300):
            yield
    finally:
        # Release only this lease; a timed-out request must not release a
        # newer one. Approvals are consumed independently before execution.
        await store.mutate(
            "lock",
            key,
            lambda value: None if value and value.get("owner") == owner else value,
            ttl=LOCK_TTL,
        )


class UiTaskStore(TaskStore):
    """The SDK's task bookkeeping, shared by the instance's workers."""

    def __init__(self, store=None):
        self.store = store or build_store()

    async def save(self, task, context=None):
        await self.store.put("task", task.id, MessageToDict(task), TASK_TTL)

    async def get(self, task_id, context=None):
        value = await self.store.get("task", task_id)
        return ParseDict(value, Task()) if value else None

    async def delete(self, task_id, context=None):
        await self.store.put("task", task_id, None)

    async def list(self, params, context=None):
        # Gemini may supply a shared transport identity. Keep bulk task listing
        # disabled; exact task lookup and continuation remain available.
        raise UnsupportedOperationError(
            "Listing all administrator tasks is unavailable"
        )
