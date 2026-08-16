"""Tests for on-disk trace persistence."""

from __future__ import annotations

import pytest

from oauthrouter.trace_store import TraceStore


@pytest.mark.asyncio
async def test_trace_store_persists_and_reloads(tmp_path):
    trace_root = tmp_path / "logs"
    trace_store = TraceStore(trace_root, max_entries=10)
    await trace_store.init()
    body_text = "x" * 40_000

    log_entry = {
        "id": "req-123",
        "timestamp": "2026-04-15T12:00:00Z",
        "method": "POST",
        "path": "/claude/v1/messages",
        "provider": "claude",
        "token_used": "claude-a",
        "status": 200,
        "elapsed_ms": 123,
        "client": "127.0.0.1",
        "has_detail": True,
        "attempts": 1,
    }
    trace = {
        "id": "req-123",
        "incoming": {
            "method": "POST",
            "body": {"text": body_text, "encoding": "utf-8"},
        },
        "attempts": [
            {
                "token_id": "claude-a",
                "response": {"status": 200},
            }
        ],
        "final": {"status": 200},
    }

    trace_store.record(log_entry, trace)
    await trace_store.persist(log_entry, trace)

    reloaded = TraceStore(trace_root, max_entries=10)
    await reloaded.init()

    assert reloaded.list_logs() == [log_entry]
    assert await reloaded.get_detail("req-123") == trace


@pytest.mark.asyncio
async def test_trace_store_truncates_large_bodies_on_disk(tmp_path):
    trace_root = tmp_path / "logs"
    trace_store = TraceStore(trace_root, max_entries=10, max_body_chars=8)
    await trace_store.init()

    log_entry = {
        "id": "req-body",
        "timestamp": "2026-04-15T12:00:00Z",
        "method": "POST",
        "path": "/claude/v1/messages",
        "provider": "claude",
        "token_used": "claude-a",
        "status": 200,
        "elapsed_ms": 12,
        "client": "127.0.0.1",
        "has_detail": True,
        "attempts": 1,
    }
    trace = {
        "id": "req-body",
        "incoming": {
            "body": {"text": "abcdefghijklmnopqrstuvwxyz", "encoding": "utf-8"}
        },
        "attempts": [],
        "final": {"status": 200},
    }

    trace_store.record(log_entry, trace)
    await trace_store.persist(log_entry, trace)

    detail = await trace_store.get_detail("req-body")
    assert detail == trace

    reloaded = TraceStore(trace_root, max_entries=10, max_body_chars=8)
    await reloaded.init()
    disk_detail = await reloaded.get_detail("req-body")

    assert disk_detail["incoming"]["body"]["text"] == "abcdefgh"
    assert disk_detail["incoming"]["body"]["text_truncated"] is True
    assert disk_detail["incoming"]["body"]["text_total_chars"] == 26


@pytest.mark.asyncio
async def test_trace_store_prunes_old_disk_entries(tmp_path):
    trace_root = tmp_path / "logs"
    trace_store = TraceStore(trace_root, max_entries=2, max_disk_entries=2)
    await trace_store.init()

    for idx in range(3):
        log_entry = {
            "id": f"req-{idx}",
            "timestamp": f"2026-04-15T12:00:0{idx}Z",
            "method": "POST",
            "path": "/claude/v1/messages",
            "provider": "claude",
            "token_used": "claude-a",
            "status": 200,
            "elapsed_ms": idx,
            "client": "127.0.0.1",
            "has_detail": True,
            "attempts": 1,
        }
        trace = {"id": f"req-{idx}", "attempts": [], "final": {"status": 200}}
        trace_store.record(log_entry, trace)
        await trace_store.persist(log_entry, trace)

    reloaded = TraceStore(trace_root, max_entries=2, max_disk_entries=2)
    await reloaded.init()

    assert [entry["id"] for entry in reloaded.list_logs()] == ["req-2", "req-1"]
    assert await reloaded.get_detail("req-0") is None
    assert await reloaded.get_detail("req-2") == {
        "id": "req-2",
        "attempts": [],
        "final": {"status": 200},
    }


@pytest.mark.asyncio
async def test_trace_store_clear_removes_memory_and_disk_entries(tmp_path):
    trace_root = tmp_path / "logs"
    trace_store = TraceStore(trace_root, max_entries=10)
    await trace_store.init()

    log_entry = {
        "id": "req-clear",
        "timestamp": "2026-04-19T18:13:55Z",
        "method": "POST",
        "path": "/openai/v1/chat/completions",
        "provider": "openai",
        "token_used": "codex",
        "status": 200,
        "elapsed_ms": 45,
        "client": "127.0.0.1",
        "has_detail": True,
        "attempts": 1,
    }
    trace = {
        "id": "req-clear",
        "incoming": {"method": "POST"},
        "attempts": [],
        "final": {"status": 200},
    }

    trace_store.record(log_entry, trace)
    await trace_store.persist(log_entry, trace)

    cleared = await trace_store.clear()

    assert cleared == 1
    assert trace_store.root == trace_root
    assert trace_store.list_logs() == []
    assert await trace_store.get_detail("req-clear") is None
    assert not (trace_root / "requests.jsonl").exists()
    assert list((trace_root / "requests").glob("*.json")) == []
