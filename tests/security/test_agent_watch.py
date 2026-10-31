"""The reference watcher must gate the event feed by server-advertised checks."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "agent_client.py"


def load_module():
    spec = importlib.util.spec_from_file_location("review_agent_client_watch", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StopWatch(Exception):
    pass


def test_watch_does_not_touch_events_outside_a_check_window(tmp_path, monkeypatch):
    module = load_module()
    calls = []

    def request(origin, path, credential=None, payload=None):
        calls.append(path)
        return json.dumps(
            {
                "scans": [
                    {
                        "kind": "picks",
                        "scan_at_new_york": "2026-10-30T08:30:00-04:00",
                        "checks": [
                            {
                                "check_id": "picks:20261030T0830:1",
                                "check_at_new_york": "2099-01-01T00:00:00-05:00",
                            }
                        ],
                    }
                ]
            }
        ).encode()

    monkeypatch.setattr(module, "request", request)
    monkeypatch.setattr(module, "consume", lambda *a, **k: pytest.fail("outside window"))
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(sleep=lambda seconds: (_ for _ in ()).throw(StopWatch())),
    )
    with pytest.raises(StopWatch):
        module.watch(
            "https://testserver",
            "credential",
            str(tmp_path / "inbox"),
            state_file=tmp_path / "state.json",
        )
    assert calls == ["/api/agent/schedule"]


def test_watch_runs_once_per_due_check_and_persists_it(tmp_path, monkeypatch):
    module = load_module()
    calls = []
    consumed = []

    def request(origin, path, credential=None, payload=None):
        calls.append(path)
        return json.dumps(
            {
                "scans": [
                    {
                        "kind": "picks",
                        "scan_at_new_york": "2020-01-01T08:30:00-05:00",
                        "checks": [
                            {
                                "check_id": "picks:20200101T0830:1",
                                "check_at_new_york": "2020-01-01T08:40:00-05:00",
                            }
                        ],
                    }
                ]
            }
        ).encode()

    def consume(*args, **kwargs):
        consumed.append(args)
        return {"saved": 0}

    sleeps = []
    monkeypatch.setattr(module, "request", request)
    monkeypatch.setattr(module, "consume", consume)
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            sleep=lambda seconds: (
                sleeps.append(seconds),
                (_ for _ in ()).throw(StopWatch()),
            )
        ),
    )
    state = tmp_path / "state.json"
    with pytest.raises(StopWatch):
        module.watch(
            "https://testserver",
            "credential",
            str(tmp_path / "inbox"),
            state_file=state,
        )
    assert len(consumed) == 1
    assert calls.count("/api/agent/schedule") >= 2
    assert "picks:20200101T0830:1" in json.loads(state.read_text())["executed"]


def test_consume_pages_until_pending_is_exhausted(tmp_path, monkeypatch):
    module = load_module()
    events = [
        {
            "id": f"{index:032x}",
            "seq": index + 1,
            "content_id": None,
            "kind": "account:expired",
            "version": None,
        }
        for index in range(101)
    ]
    pages = []
    after = 0

    def request(origin, path, credential=None, payload=None):
        nonlocal after
        assert "after=" in path
        after = int(path.rsplit("after=", 1)[1])
        batch = [event for event in events if event["seq"] > after][:100]
        return json.dumps(
            {
                "events": batch,
                "next_cursor": batch[-1]["seq"] if batch else after,
            }
        ).encode()

    monkeypatch.setattr(module, "request", request)
    result = module.consume(
        "https://testserver", "credential", str(tmp_path / "inbox")
    )
    assert result["saved"] == 101
    assert result["pages"] == 2
    assert len(list((tmp_path / "inbox").rglob("event.json"))) == 101
