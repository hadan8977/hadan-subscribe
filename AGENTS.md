# Hadan Subscribe

Independent rewrite of quantcheck as a small private content subscription product.
The production quantcheck checkout and /opt/quantcheck must remain untouched.

## Product invariants
- Email identifies a user; stable user IDs own subscriptions and pending events.
- Authorization fails closed; user, subscription, binding and attachment access are checked server side.
- Email preferences and agent preferences are independent. Unsubscribe must win over queued retries.
- Provider acceptance is not processing confirmation. Retry only with a recorded idempotency identity; uncertain SMTP outcomes require reconciliation.
- No credentials in public prompts, URLs, git, logs, browser storage or review artifacts.
- Local/test mail uses a filesystem sink; no real recipient sends during development.
- Maintenance agents operate in an isolated checkout without production secrets. Policy and verification are enforced outside model instructions.

## Owner approval for outbound email

- Never send a push, report or announcement email to all users, or to any non-admin recipient, without the owner's explicit approval for that specific send.
- Before asking for approval, send the exact rendered email (subject, HTML/text body and every attachment) to the configured admin addresses and wait for the owner to review it.
- After approval, perform a final double check immediately before sending: recipient count, opt-outs and suppressions, content version, attachments and the exact approved format. Approval for one send is not approval for any later send.
- During debugging, development, testing, migrations and format changes, default to admin-only email. Use `HADAN_ADMIN_EMAILS` as the only allowed recipients unless the owner has explicitly approved a user send.
- A successful test send, a passing job or a previous approval does not authorize a new user-facing send.
- Never use a historical recipient list, the retired quantcheck recipient files, or an inferred audience to bypass this rule.

## Working practices
- Keep the core deployable on a single small Linux server. Optional browser/model integrations load only when needed.
- Prefer standard library and server-rendered HTML; justify additional services/dependencies.
- Run meaningful user-flow, authorization, crash recovery, concurrency and migration tests.
- Document implemented, locally tested and environment-blocked capabilities separately.
- Subagent reviews must cover functionality, user experience, operational reliability and security. Fix findings and rerun affected checks.
