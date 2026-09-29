"""Worker-shared state on the instance's disk; no database or network calls."""

import asyncio

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import Task
from a2a.utils.errors import UnsupportedOperationError

from ping_admin_agent import ui_store
from ping_admin_agent.ui_actions import UiActionError
from ping_admin_agent.ui_store import (
    MemoryStore,
    SqliteStore,
    UiTaskStore,
    build_store,
    conversation_lock,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryStore()
    return SqliteStore(tmp_path / "state.sqlite3")


@pytest.mark.asyncio
async def test_expired_controls_cannot_be_used(monkeypatch, store):
    now = 1000
    monkeypatch.setattr(ui_store.time, "time", lambda: now)
    await store.put("surface", "control", {"target": "u1"})
    assert await store.get("surface", "control") == {"target": "u1"}
    now += ui_store.SURFACE_TTL
    assert await store.get("surface", "control") is None
    await store.put("surface", "fresh", {"target": "u2"})
    assert await store.get("surface", "control") is None
    assert await store.get("surface", "fresh") == {"target": "u2"}


@pytest.mark.asyncio
async def test_request_cancellation_releases_conversation(store):
    entered = asyncio.Event()

    async def request():
        async with conversation_lock(store, "conversation"):
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(request())
    try:
        await entered.wait()
        with pytest.raises(UiActionError, match="already running"):
            async with conversation_lock(store, "conversation"):
                pytest.fail("A second request entered the same conversation")
        async with conversation_lock(store, "other-conversation"):
            pass
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    async with conversation_lock(store, "conversation"):
        pass


@pytest.mark.asyncio
async def test_workers_share_controls_and_consume_approvals_once(tmp_path):
    # Two worker processes of one instance open the same file on its disk.
    first = SqliteStore(tmp_path / "state.sqlite3")
    second = SqliteStore(tmp_path / "state.sqlite3")
    await first.put("surface", "view", {"actions": {"c1": {"consumed": False}}})
    assert await second.get("surface", "view") == {
        "actions": {"c1": {"consumed": False}}
    }

    def consume(value):
        if not value or value["consumed"]:
            raise UiActionError("Already used")
        return {**value, "consumed": True}

    await first.put("approval", "token", {"consumed": False})
    outcomes = await asyncio.gather(
        *[
            worker.mutate("approval", "token", consume)
            for worker in (first, second, first, second)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, UiActionError) for outcome in outcomes) == 3
    assert (await second.get("approval", "token"))["consumed"] is True
    # The conversation lease is shared as well.
    async with conversation_lock(first, "conversation"):
        with pytest.raises(UiActionError, match="already running"):
            async with conversation_lock(second, "conversation"):
                pass
    async with conversation_lock(second, "conversation"):
        pass


@pytest.mark.asyncio
async def test_failed_updates_leave_the_record_unchanged(tmp_path):
    store = SqliteStore(tmp_path / "state.sqlite3")
    await store.put("pending", "key", {"decisions": {}})

    def fail(value):
        raise UiActionError("rejected")

    with pytest.raises(UiActionError):
        await store.mutate("pending", "key", fail)
    assert await store.get("pending", "key") == {"decisions": {}}
    await store.put("pending", "key", None)
    assert await store.get("pending", "key") is None


def test_managed_hosts_share_state_and_reject_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "7991177")
    monkeypatch.setenv("PING_ADMIN_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PING_ADMIN_STATE_STORE", raising=False)
    first, second = build_store(), build_store()
    assert isinstance(first, SqliteStore)
    assert first is second
    assert first.path == tmp_path / "7991177.sqlite3"
    monkeypatch.setenv("PING_ADMIN_STATE_STORE", "memory")
    with pytest.raises(ValueError, match="sqlite"):
        build_store()
    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    assert isinstance(build_store(), MemoryStore)


@pytest.mark.asyncio
async def test_protocol_tasks_are_shared_and_bulk_listing_is_disabled(tmp_path):
    context = ServerCallContext()
    first = UiTaskStore(SqliteStore(tmp_path / "state.sqlite3"))
    second = UiTaskStore(SqliteStore(tmp_path / "state.sqlite3"))
    await first.save(Task(id="task", context_id="conversation"), context)
    assert (await second.get("task", context)).context_id == "conversation"
    await second.delete("task", context)
    assert await first.get("task", context) is None
    with pytest.raises(UnsupportedOperationError):
        await first.list(None, context)
