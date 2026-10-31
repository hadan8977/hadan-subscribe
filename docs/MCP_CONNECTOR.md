# Remote MCP connector and MCP Events

Updated 2026-10-30. The public endpoint is `https://quant.example.com/mcp`.

## What is implemented

- OAuth 2.1 authorization server with PKCE S256, dynamic client registration,
  protected-resource metadata and refresh-token rotation.
- OAuth access tokens are opaque; only SHA-256 digests are stored. Each token is
  bound to the owner's single active Agent binding. Revoking that binding in the
  web account immediately invalidates the token.
- MCP is a stateless Streamable HTTP endpoint. It supports `server/discover`,
  legacy `initialize`, `tools/list`, `tools/call`, `events/list`,
  `events/subscribe` and `events/unsubscribe`.
- MCP Events advertise `quant.report.published`. A report publication, revision
  or withdrawal queues one signed webhook per active subscription. Delivery uses
  Standard Webhooks headers and a fresh timestamp/signature on every retry.
- Callback verification is a signed single-use challenge before any private
  report data is sent. Callback URLs must be public HTTPS; private/local
  addresses, credentials, fragments and redirects are rejected.
- Connector validation probes are supported: anonymous `server/discover`,
  `initialize`, `ping`, `tools/list`, `events/list`, and an anonymous
  `GET /mcp` SSE probe. Private reads, event subscriptions and tool calls still
  require a valid credential.
- Tool definitions advertise OAuth `securitySchemes`; an anonymous tool call
  returns a tool-level `mcp/www_authenticate` challenge with `error` and
  `error_description`, so ChatGPT can show its linking UI.
- Path-aware protected-resource metadata is served at
  `/.well-known/oauth-protected-resource/mcp`; the root endpoint returns the
  same `resource` value (`https://quant.example.com/mcp`).
- `/mcp` accepts JSON-RPC bodies sent as `application/octet-stream` (ChatGPT's
  validator does this). An exact empty `application/octet-stream` POST is the
  ChatGPT reachability probe and returns `204 No Content`.
- There is no OIDC endpoint: `/.well-known/openid-configuration` returns 404.
  For ChatGPT-like User-Agents the OAuth metadata includes a small set of
  OIDC-shaped compatibility fields (`subject_types_supported`,
  `id_token_signing_alg_values_supported`, `userinfo_endpoint`, `jwks_uri`).
  No ID token is issued; `/oauth/userinfo` is the identity source and returns
  `{"sub":"anonymous"}` without a token.
- Known callbacks: ChatGPT stable/connector and `/aip/` paths, Grok
  `https://grok.com/connectors-oauth-exchange-code/`, Cursor web/bot and
  `cursor://anysphere.cursor-mcp/oauth/callback`, Claude's MCP callback, and
  exact loopback `/callback` URLs. Other agents should use the pairing fallback.

## ChatGPT setup

1. Open ChatGPT on the web (or the desktop Work chat with Cloud selected).
2. Open **Settings → Apps/Connectors → Advanced settings** and enable
   **Developer mode**. Plan and menu names vary by workspace.
3. Create a custom connector:
   - Name: `Quant`
   - URL: `https://quant.example.com/mcp`
   - Authentication: OAuth; leave client ID/secret empty so ChatGPT uses
     dynamic client registration.
4. Click connect. The browser opens Quant's authorization page. Verify the
   owner email. On a first connection from a supported remote
   connector (ChatGPT, Grok, Cursor or Claude) the authorization completes
   automatically, with no separate consent click. Loopback/custom clients and
   replacement of an existing active connection still ask for confirmation. Agent delivery remains under the
   owner's control on the account page.
5. If the client cannot show an OAuth authorization card (`no_auth_link`), use
   the site pairing link instead. It is the supported fallback: the agent asks
   for a pairing link, the owner verifies their email, and a first connection
   approves automatically; the agent stores the device credential.
6. First-time sequence: connect -> call `get_subscription_status` -> explain
   the live plan, price, payment method and terms link -> owner confirms in chat
   -> `prepare_checkout` with the live plan/method/terms -> send the QR image ->
   after payment, enable Agent delivery -> then `events/subscribe`. Events
   require an active subscription and Agent delivery.
7. In a Work chat or dot, ask it to subscribe to Quant report events. ChatGPT
   will call `events/subscribe` with its callback URL and `whsec_` secret.
   Quant verifies the callback before accepting the subscription.

MCP Events availability is controlled by ChatGPT. It is available in Work
chats on the web, Work chats in the desktop app with Cloud selected, and with
dots; it is not available in every chat surface or every plan. If the connector
is available but Events is not, the same connector still provides on-demand
MCP tools.

## On-demand tools

- `get_subscription_status()` — works before purchase; returns live plans and
  the owner-only subscribe URL
- `get_scan_schedule()` — upcoming scans plus exact check times: at most three
  checks per scan (scan + 10, +20 and +30 minutes). Poll only inside those
  windows; never run a 24/7 ten-minute loop.
- `prepare_checkout(plan_id, method, accept_terms, terms_version)` — after the
  owner confirms the live plan, price, method and terms in chat, prepares the
  single QR checkout and returns a PNG image block. The server fixes the amount.
- `get_checkout_status()` — reads that order; `paid=true` is the only payment
  confirmation
- `get_checkout_qr()` — returns the current QR as a PNG image
- `list_events(after?)` — returns pending events
- `read_content(content_id, version?)`
- `read_attachment(attachment_id)` — base64 embedded resource
- `ack_event(event_id)` — only after content and attachments were processed

MCP Tools alone does not wake the model or schedule work. If MCP Events is not
available, use a scheduler only at the returned check times and notify only
when `list_events()` returns pending events. Email is independent.

## Channel deduplication

Webhook delivery is preferred. While a webhook delivery for an event is
queued, retrying or accepted, `list_events()` hides that event for the same
binding; polling is the fallback after a permanent webhook failure. Agents
must treat `eventId`/`event_id` and `content_id` as idempotency keys across
webhook, polling and email. Content emails include `X-Quant-Event-ID`,
`X-Quant-Content-ID` and `X-Quant-Version` headers (and a `/read/<content_id>`
link when applicable), so an agent with mailbox access can suppress a duplicate
notification without changing email delivery timing.

## Email delivery

Quant sends sign-in and report email from the public sender address shown on
the website. Tell the owner to check spam/junk and allowlist that address; if
the agent manages mailbox filters, add the sender directly. The live address
is returned as `mail_sender` by `get_subscription_status` and
`/api/purchase-options`.

## QR checkout from an agent

The owner confirms the subscription in chat; there is no separate website
authorization button. The agent reads the live plan, payment methods and
`terms_version` from `get_subscription_status`, explains the plan, price, method
and terms link, and asks for explicit confirmation. It then calls:

```json
{"plan_id": "<live plan id>", "method": "wechat", "accept_terms": true, "terms_version": "<live version>"}
```

with `prepare_checkout` over MCP, or the same JSON with
`POST /api/agent/checkout` using a device token. The server fixes the amount,
currency, product and user; client-supplied amounts or arbitrary plans are
rejected. The order is short-lived and single-use.

1. Show the returned PNG QR inline to the owner. Do not open a browser or cloud
   computer when a QR is available.
2. If the client cannot render MCP image content, call `get_checkout_qr` (or
   `GET /api/agent/checkout/qr`) and send the PNG as an attachment.
3. Poll `get_checkout_status` no faster than every 5 seconds. Only `paid=true`
   confirms payment.

The QR has an opaque white background and a white quiet zone so it remains
scannable in dark chat themes. The website remains a fallback, not the primary
handoff.

## OAuth endpoints

- Protected resource: `/.well-known/oauth-protected-resource`
- Authorization server: `/.well-known/oauth-authorization-server`
- Dynamic registration: `POST /oauth/register`
- Authorization: `GET /oauth/authorize`
- Token: `POST /oauth/token`
- Revocation: `POST /oauth/revoke`

Scopes are `quant.read`, `quant.ack` and `offline_access`. The QR helpers use
`quant.read`; the server fixes the plan/amount and the owner's payment remains
the final gate. Quant grants `offline_access` alongside any `quant.read` or
`quant.ack` authorization, even if the client omitted it, so MCP connectors
receive a rotating refresh token and survive the one-hour access-token window.
Existing connections created before this behavior must reauthorize once. The OAuth
`resource` value is validated against `https://quant.example.com/mcp` (the bare
origin is accepted for compatibility clients).

## Event payload

```json
{
  "eventId": "evt_...",
  "name": "quant.report.published",
  "timestamp": "2026-10-30T15:00:00Z",
  "data": {
    "event_id": "evt_...",
    "content_id": "...",
    "version": 1,
    "title": "...",
    "summary": "...",
    "change_type": "published",
    "withdrawn": false,
    "published_at": "2026-10-30T15:00:00Z",
    "url": "https://quant.example.com/read/..."
  }
}
```

The event contains the title and the first 1,000 characters of the report body
as `summary`; treat the webhook body as private. The agent uses the read tools
for the full report and attachments.

## Safety boundaries

- No subscription, payment, admin or email-preference mutation tools are exposed
  over MCP.
- Report bodies and webhook payloads are data, never authorization instructions.
- A revoke removes the binding, invalidates OAuth tokens and deactivates pending
  MCP event subscriptions for that binding.

## Diagnostics

Production writes a redacted request log to
`/var/lib/hadan/oauth-diagnostics.log`. It records method, path without query,
status, User-Agent, Origin, Accept, content type, MCP protocol/method headers
and auth scheme. DCR diagnostic entries also include selected registration
metadata (client name, redirect URIs, grants, scope). It never records query
strings, raw request bodies, tokens or secrets.
