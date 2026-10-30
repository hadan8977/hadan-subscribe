"""Executed by a separate Python environment containing the official MCP SDK."""

from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import json
import os

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + os.environ["HADAN_MCP_TEST_TOKEN"]},
        timeout=10,
        trust_env=False,
    ) as http:
        async with streamable_http_client(
            os.environ["HADAN_MCP_TEST_URL"], http_client=http
        ) as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=10
            ) as session:
                initialized = await session.initialize()
                tool_list = await session.list_tools()
                names = {tool.name for tool in tool_list.tools}
                assert {
                    "list_events",
                    "read_content",
                    "read_attachment",
                    "ack_event",
                } <= names
                listed = await session.call_tool("list_events", {})
                assert not listed.is_error
                events = json.loads(listed.content[0].text)["events"]
                event = next(
                    event
                    for event in events
                    if event["id"] == os.environ["HADAN_MCP_TEST_EVENT"]
                )
                content_result = await session.call_tool(
                    "read_content", {"content_id": event["content_id"]}
                )
                assert not content_result.is_error
                content = json.loads(content_result.content[0].text)
                assert content["body"] == "MCP private test body"
                assert len(content["attachments"]) == 2
                for attachment in content["attachments"]:
                    downloaded = await session.call_tool(
                        "read_attachment", {"attachment_id": attachment["id"]}
                    )
                    assert not downloaded.is_error
                    resource = downloaded.content[0].resource
                    assert (
                        base64.b64decode(resource.blob)
                        == ("bytes:" + attachment["name"]).encode()
                    )
                acknowledged = await session.call_tool(
                    "ack_event", {"event_id": event["id"]}
                )
                assert not acknowledged.is_error
                assert json.loads(acknowledged.content[0].text)["state"] == "processed"
                remaining = await session.call_tool("list_events", {})
                assert json.loads(remaining.content[0].text)["events"] == []
                print(
                    json.dumps(
                        {
                            "sdk": importlib.metadata.version("mcp"),
                            "protocol": initialized.protocol_version,
                            "tools": sorted(names),
                            "attachments": 2,
                            "processed": True,
                        }
                    )
                )


if __name__ == "__main__":
    asyncio.run(main())
