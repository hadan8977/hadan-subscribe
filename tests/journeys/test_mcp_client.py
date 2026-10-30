from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn


def test_mcp_initialize_list_read_and_permission_errors(journey):
    reader = journey.login()
    journey.subscribe(reader)
    _, agent = journey.connect(reader)
    item = journey.service.publish("quantgt", "mcp-json", "MCP report", "Private body")

    def rpc(method, params=None, identifier=1):
        return agent.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": identifier,
                "method": method,
                "params": params or {},
            },
        )

    unauthenticated = journey.client().post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert unauthenticated.status_code == 200
    assert any(t["name"] == "read_content" for t in unauthenticated.json()["result"]["tools"])
    initialized = rpc(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "journey", "version": "1"},
        },
    )
    assert (
        initialized.status_code == 200
        and "tools" in initialized.json()["result"]["capabilities"]
    )
    notification = agent.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert notification.status_code == 202
    names = {tool["name"] for tool in rpc("tools/list").json()["result"]["tools"]}
    assert {"list_events", "read_content", "read_attachment", "ack_event"} <= names
    assert rpc("unknown/method").json()["error"]["code"] == -32601
    result = rpc(
        "tools/call", {"name": "read_content", "arguments": {"content_id": item["id"]}}
    ).json()["result"]
    assert json.loads(result["content"][0]["text"])["body"] == "Private body"
    forbidden = rpc(
        "tools/call",
        {"name": "read_content", "arguments": {"content_id": "unowned-or-nonexistent"}},
    ).json()["result"]
    assert forbidden["isError"] is True


def test_official_mcp_sdk_over_real_local_http(journey):
    python = os.environ.get("MCP_SDK_PYTHON")
    if not python:
        pytest.skip(
            "Set MCP_SDK_PYTHON to a separate Python environment with official mcp==2.3.0; runtime does not require it"
        )
    if not Path(python).is_file():
        pytest.fail("MCP_SDK_PYTHON must point to an existing interpreter")
    reader = journey.login()
    journey.subscribe(reader)
    binding, _ = journey.connect(reader)
    item = journey.service.publish(
        "quantgt",
        "mcp-sdk",
        "SDK report",
        "MCP private test body",
        files=[
            {"name": name, "data": ("bytes:" + name).encode()}
            for name in ("report.txt", "notes.txt")
        ],
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        journey.app, log_level="critical", access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "Local test server failed to start"
        result = subprocess.run(
            [python, str(Path(__file__).with_name("mcp_sdk_client.py"))],
            env={
                "HADAN_MCP_TEST_URL": f"http://127.0.0.1:{port}/mcp",
                "HADAN_MCP_TEST_TOKEN": binding["token"],
                "HADAN_MCP_TEST_EVENT": item["event_id"],
                "PYTHONNOUSERSITE": "1",
            },
            text=True,
            capture_output=True,
            timeout=35,
        )
        assert result.returncode == 0, result.stderr[-5000:]
        report = json.loads(result.stdout.strip())
        assert report["processed"] is True and report["attachments"] == 2
        assert (
            journey.service.db.one(
                "SELECT state FROM receipts WHERE event_id=?", (item["event_id"],)
            )["state"]
            == "processed"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "Local MCP test server did not stop"
