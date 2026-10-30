#!/usr/bin/env python3
"""Optional stdio bridge for clients that cannot configure Streamable HTTP headers."""

import json
import os
import sys
from pathlib import Path
from agent_client import request

origin = os.environ["HADAN_AGENT_URL"].rstrip("/")
path = Path(os.environ["HADAN_AGENT_TOKEN_FILE"])
if path.stat().st_mode & 0o077:
    raise SystemExit("Credential file must have mode 0600")
credential = path.read_text().strip()
for line in sys.stdin:
    message = None
    try:
        message = json.loads(line)
        response = request(origin, "/mcp", credential, message)
        if response:
            print(response.decode(), flush=True)
    except Exception as exc:
        # Never echo request payload or credential on stdout/stderr.
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id") if isinstance(message, dict) else None,
                    "error": {"code": -32603, "message": type(exc).__name__},
                }
            ),
            flush=True,
        )
