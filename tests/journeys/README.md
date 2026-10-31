# User journey acceptance tests

These tests create temporary SQLite databases and `.eml` files only. No production environment, real email recipient, payment account or model credential is used.

Run the normal suite from the project root:

```bash
.venv/bin/python -m pytest -q tests/journeys
```

The independent official MCP SDK check uses a separate environment to keep the SDK out of production dependencies:

```bash
.venv/bin/python -m venv /tmp/hadan-mcp-sdk-review
uv pip install --python /tmp/hadan-mcp-sdk-review/bin/python 'mcp==2.3.0'
MCP_SDK_PYTHON=/tmp/hadan-mcp-sdk-review/bin/python .venv/bin/python -m pytest -q tests/journeys/test_mcp_client.py
```

The SDK test starts the real application on an ephemeral loopback port, initializes the protocol, discovers tools, reads an event and private body, downloads two embedded attachments, acknowledges processing and verifies the pending list clears. It stops the local server afterward. It proves compatibility with that official SDK version; it does not prove background execution in a specific Personal Agent product or OAuth interoperability.

Fault injection covers duplicate publishing, permanent local message identity, retry backoff, opt-out before retry, delivery acceptance versus processing, stale worker leases, uncertain SMTP state, two-worker claims, revision supersession and email independence after an Agent acknowledgement. The SMTP cases use an injected transport or inspect recovery state without opening a SMTP connection.

## Portable browser review

Run in a writable test checkout; the script creates its own temporary database, loopback HTTP server and local mailbox, and closes its browser/server afterward:

```bash
PYTHONPATH=src .venv/bin/python tests/journeys/browser_smoke.py
```

The script never installs a browser. It selects an executable in this order:

1. `HADAN_TEST_CHROMIUM`, when explicitly set to an existing executable. An invalid override fails visibly.
2. The installed executable for the current Playwright package, honoring `PLAYWRIGHT_BROWSERS_PATH`.
3. An existing Chromium/Chrome on `PATH`, then a browser already present in the configured or user Playwright cache.

The selected path, browser version and selection method are recorded in ignored `test-results/ux/report.json`. The matching Playwright browser is preferred; a fallback is useful for an existing host but its compatibility is established only by the test result. Non-root runs enable Chromium's sandbox. Root runs are limited to this local synthetic-data test and use Playwright without the Chromium sandbox; do not use that as the production deployment model.

An explicit host binary can be selected without changing source code:

```bash
HADAN_TEST_CHROMIUM=/usr/bin/chromium PYTHONPATH=src .venv/bin/python tests/journeys/browser_smoke.py
```

On a new development/QA machine, install dependencies and the matching browser first:

```bash
uv sync --frozen --extra test
.venv/bin/python -m playwright install --with-deps chromium
```

For a Linux deployment layout, an administrator can install the system libraries and browser outside the application release directory after installing the source/test extras in the current release's `/opt/quant-subscribe/current/.venv`:

```bash
sudo install -d -o root -g root -m 0755 /opt/quant-subscribe/browsers
sudo /opt/quant-subscribe/current/.venv/bin/python -m playwright install-deps chromium
sudo env PLAYWRIGHT_BROWSERS_PATH=/opt/quant-subscribe/browsers /opt/quant-subscribe/current/.venv/bin/python -m playwright install chromium
```

Use `PLAYWRIGHT_BROWSERS_PATH=/opt/quant-subscribe/browsers` in the test/deployment environment as well. Keep each release's virtualenv with its code so rollback switches both; keep the browser revisions needed by retained releases. Run QA from a separate writable checkout, keep production releases read-only and rebuild virtualenvs on the destination server. The official [Playwright browser guide](https://playwright.dev/python/docs/browsers) describes browser-version matching, system dependencies and custom installation locations.

The browser flow also verifies imported/baseline archives, private account notices without content links, an Agent acknowledgement changing the displayed processing state, and another subscriber being unable to see that notice. Screenshots mask displayed OTPs and Agent credentials; no cookie/storage/HAR/trace files are exported.

The current Quant UI flow exercises Chinese and English, in-place language changes during OTP entry, route/version preservation, real temporary access-code issuance and redemption, independent email/Agent delivery settings, revision and attachment downloads, replacement-Agent activation, opt-out-preserving renewal, and admin withdrawal/replay. It checks desktop, 390 px and 320 px layouts. Access codes are also masked in screenshots. The loopback port is allocated above browser-blocked service ports without disabling Chromium's unsafe-port protection.

Payments are unconfigured during the actual service journey, so no invented price or purchase form is shown. A final explicitly synthetic API fixture checks configured-method rendering, pending/verification/manual-review/paid states and duplicate-payment prevention; it does not prove a live provider transaction or callback. No request is allowed outside the disposable loopback origin.

For a shorter follow-up covering versioned private URLs through OTP login, accurate payment-resolution translations, terminal order UI and rejected external `next` URLs, run:

```bash
PYTHONPATH=src .venv/bin/python tests/journeys/browser_i18n_edges.py
```

It writes `test-results/ux/edge-report.json`, uses its own temporary server and mailbox, and does not replace the broader browser journey or prove a live payment.

For email-first subscription onboarding, entitlement, opt-out preservation, private deep links and out-of-order feed response regression:

```bash
PYTHONPATH=src .venv/bin/python tests/journeys/browser_onboarding.py
```

This creates a disposable file-mail environment and sends no real messages. Its feed-race scenario holds the older unauthorized response until the newly authorized report is visible, then verifies it cannot erase the newer report.


For the isolated public preview (no database, real mail or payment requests):

```bash
PYTHONPATH=src .venv/bin/python tests/journeys/browser_preview.py
```

It starts its own read-only server, verifies both complete UI paths, asserts zero API network requests, checks the minimal clipboard text and exercises the simulated conversation in three languages and capability modes. The server is stopped afterward.
