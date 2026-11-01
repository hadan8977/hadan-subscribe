# Project invariants

- Email identifies a user; stable user IDs own subscriptions, receipts and pending events.
- Authorization fails closed. User, subscription, binding, checkout and attachment access are checked server-side.
- Email preferences and agent preferences are independent; an explicit opt-out must win over queued retries.
- Provider acceptance is not processing confirmation. Retry with a recorded idempotency identity; reconcile uncertain SMTP outcomes.
- QR checkout is owner-confirmed and single-use. After the owner confirms the live plan, price, method and terms in chat, an agent may prepare and display that QR; it never chooses the amount, pays, redeems or mutates a subscription.
- Never commit credentials, `.env` files, runtime databases, private keys, production addresses or customer data.
- Maintenance agents operate in an isolated checkout without production secrets. Policy and verification are enforced outside model instructions.
- Keep the core deployable on one small Linux server. Run focused tests for each change; use the full suite only for broad refactors.

## Owner approval for outbound email

- Never send a push, report or announcement email to all users, or to any non-admin recipient, without the owner's explicit approval for that specific send.
- Before asking for approval, send the exact rendered email (subject, HTML/text body and every attachment) to the configured admin addresses and wait for the owner to review it.
- After approval, perform a final double check immediately before sending: recipient count, opt-outs and suppressions, content version, attachments and the exact approved format. Approval for one send is not approval for any later send.
- During debugging, development, testing, migrations and format changes, default to admin-only email. Use `HADAN_ADMIN_EMAILS` as the only allowed recipients unless the owner has explicitly approved a user send.
- A successful test send, a passing job or a previous approval does not authorize a new user-facing send.
- Never use a historical recipient list, the retired quantcheck recipient files, or an inferred audience to bypass this rule.
