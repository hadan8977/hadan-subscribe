# Offline legacy acceptance tests

Adapted from the MIT-licensed quantcheck test fixtures for the retained pure parsers, validation, diff policy, report formatting, email presentation, Excel layout and browser-facing extraction helpers. Tests import only `hadan`; no legacy checkout, environment file, production data, real network or browser is used. Excel artifacts are written into temporary directories.

Run with the project's test extras installed:

```sh
python -m pytest tests/legacy -q
```

Deliberate differences from old tests:

- The old fingerprint-before-send dedupe test is excluded. Durable event/outbox tests must cover dedupe without preserving the delivery-loss defect.
- Mail-template tests call the pure renderer directly; the discarded orchestration wrapper's internal `context` argument never belonged in subscriber content.
- The workbook Symbol/sheet contract is verified directly using openpyxl, without importing the legacy historical-send command.
- Scheduler, membership fail-open, renewal restoring email consent and provider fallback semantics are not copied.
- Browser lifecycle/screenshot orchestration changed to explicit configuration and is tested separately when that interface is available; fake-page detail/auth tests preserve extraction behavior.

Two additional acceptance guards deliberately expose existing unsafe behavior rather than preserving it: JavaScript regex word boundaries must survive Python escaping, and untrusted source strings must not become Excel formulas. Failures in these guards are reported separately from report/parser parity regressions.

The expanded suite also exercises durable source publishing and replay, digest corrections, fake IMAP ingestion, NYSE/DST scheduling, member import, bulk edits, retention/publication concurrency and bounded parser maintenance. Maintenance tests use fake model/verifier responses and fake browser captures; they do not establish that DeepSeek, bubblewrap or a live source account is usable on a deployment host. The fixture in `tests/fixtures/quantgt_sample.json` contains only invented report data.
