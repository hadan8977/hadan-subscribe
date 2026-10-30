# Quant Subscribe

A small, single-server private subscription service for people and their AI agents. Email identifies the owner; subscriptions and reports are private; delivery can use email, paired device credentials, or a remote MCP connector with OAuth and optional MCP Events webhooks.

## What is included

- Email OTP login, subscriptions, fixed-date plans, redemption codes and WeChat/Xiaodou integrations
- Private report versions and attachments with authorized downloads
- Email delivery and agent delivery as independent channels
- Pairing-based device credentials for HTTPS polling agents
- OAuth 2.1 + PKCE + DCR for remote MCP clients
- MCP Tools and MCP Events (`quant.report.published`) with signed webhooks
- Owner-approved QR checkout helpers over MCP and the device API
- Admin-only member management through a loopback MCP service
- SQLite storage, encrypted backups, health checks and systemd deployment templates

## Local setup

```bash
uv sync --extra test
cp .env.example .env
# edit .env; keep MAIL_BACKEND=file for local work
uv run hadan init
uv run hadan serve --host 127.0.0.1 --port 8080
```

Local mail is written to `var/mail/`; no real email is sent with `MAIL_BACKEND=file`.

Run the tests relevant to a change, for example:

```bash
uv run pytest tests/security/test_xiaodou.py -q
uv run pytest tests/journeys/test_oauth_mcp_events.py -q
```

## Deployment

`deploy/` contains generic systemd units, a Caddy example and an optional isolated ingress example. Replace `quant.example.com`, paths and environment values with your own. Never commit `.env`, runtime data, SQLite files, SMTP credentials, payment private keys or OAuth signing material.

See `docs/MCP_CONNECTOR.md` for the remote MCP/OAuth contract and `docs/AGENT_SUBSCRIPTION_FLOW.md` for agent onboarding. Production-specific deployment records are intentionally not part of this repository.
