# Project invariants

- Email identifies a user; stable user IDs own subscriptions, receipts and pending events.
- Authorization fails closed. User, subscription, binding, checkout and attachment access are checked server-side.
- Email preferences and agent preferences are independent; an explicit opt-out must win over queued retries.
- Provider acceptance is not processing confirmation. Retry with a recorded idempotency identity; reconcile uncertain SMTP outcomes.
- QR checkout is owner-approved and single-use. Agents may prepare and display that QR, but never choose a plan or amount, accept terms, pay, redeem or mutate a subscription.
- Never commit credentials, `.env` files, runtime databases, private keys, production addresses or customer data.
- Maintenance agents operate in an isolated checkout without production secrets. Policy and verification are enforced outside model instructions.
- Keep the core deployable on one small Linux server. Run focused tests for each change; use the full suite only for broad refactors.
