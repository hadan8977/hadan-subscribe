"""Independent maintenance acceptance: constrained parser edits and promotion gates."""

import json
import copy
from contextlib import contextmanager
from pathlib import Path
import subprocess

import pytest

from hadan import ops
from hadan.worker import Worker
from hadan.sources import jobs
from hadan.transport import DeliveryError


ORIGINAL = 'import re\nLABEL = "Updated on"\ndef parse(text):\n    return re.search(r"Updated on [A-Z]+", text)\n'
CANDIDATE = 'import re\nLABEL = "Updated"\ndef parse(text):\n    return re.search(r"Updated (?:on )?[A-Z]+", text)\n'


def test_literal_policy_accepts_regex_and_label_adaptation():
    assert ops.literal_only(ORIGINAL, CANDIDATE)


@pytest.mark.parametrize(
    "before,after",
    [
        (ORIGINAL, ORIGINAL.replace("re.search", "re.match")),
        (ORIGINAL, ORIGINAL + '\nprint("unexpected side effect")\n'),
        ("LIMIT=8\n", "LIMIT=1\n"),
        ("VALID=True\n", "VALID=False\n"),
        ('VALUE=b"old"\n', 'VALUE=b"new"\n'),
        (
            'def f(value): return f"old {value}"\n',
            'def f(value): return f"new {secret}"\n',
        ),
        (ORIGINAL, "def syntax(:\n"),
    ],
)
def test_literal_policy_rejects_executable_changes(before, after):
    assert not ops.literal_only(before, after)


@pytest.fixture
def parser_proposal(source_service, tmp_path, monkeypatch):
    service = source_service
    service.settings.ops_ai = True
    service.settings.deepseek_key = "fixture-not-an-api-key"
    checkout = tmp_path / "checkout"
    parser = checkout / "src/hadan/compat/scrape_parse.py"
    parser.parent.mkdir(parents=True)
    parser.write_text(ORIGINAL)
    monkeypatch.setenv("OPS_CHECKOUT", str(checkout))
    monkeypatch.setenv("OPS_MAX_CALLS_PER_DAY", "10")
    incident = Worker(service).incident(
        "source:picks", "Source date label changed", "fixture context"
    )
    return service, incident


def fake_verifier(monkeypatch, text=CANDIDATE, passed=True, files=None):
    def verify(checkout, patch, destination):
        path = Path(destination) / "src/hadan/compat/scrape_parse.py"
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return {
            "passed": passed,
            "files": files or ["src/hadan/compat/scrape_parse.py"],
            "tests": 81,
        }

    monkeypatch.setattr(ops, "verify_patch", verify)


def proposal_response(_):
    return json.dumps(
        {
            "patch": "fixture diff handled only by fake verifier",
            "reason": "fixture source label",
        }
    )


def test_only_verified_literal_candidate_reaches_source_canary(
    parser_proposal, monkeypatch
):
    service, incident = parser_proposal
    fake_verifier(monkeypatch)
    result = ops.propose_parser(service, incident, client=proposal_response)
    assert result["passed"]
    tasks = service.db.all("SELECT * FROM jobs WHERE kind='source:activate_parser'")
    assert len(tasks) == 1
    payload = json.loads(tasks[0]["payload"])
    assert payload["sha256"] == ops.digest(CANDIDATE)
    assert Path(payload["candidate"]).is_relative_to(service.settings.data_dir / "ops")


def test_failed_verification_never_queues_activation(parser_proposal, monkeypatch):
    service, incident = parser_proposal
    fake_verifier(monkeypatch, passed=False)
    result = ops.propose_parser(service, incident, client=proposal_response)
    assert not result["passed"]
    assert not service.db.all("SELECT * FROM jobs WHERE kind='source:activate_parser'")


def test_ast_change_is_rejected_even_if_fake_verifier_reports_pass(
    parser_proposal, monkeypatch
):
    service, incident = parser_proposal
    fake_verifier(monkeypatch, text=ORIGINAL.replace("re.search", "re.match"))
    with pytest.raises(ValueError, match="structure"):
        ops.propose_parser(service, incident, client=proposal_response)
    assert not service.db.all("SELECT * FROM jobs WHERE kind='source:activate_parser'")


def test_model_insufficient_evidence_never_enters_verifier(
    parser_proposal, monkeypatch
):
    service, incident = parser_proposal
    monkeypatch.setattr(
        ops,
        "verify_patch",
        lambda *args: pytest.fail("No patch must not start verification"),
    )
    result = ops.propose_parser(
        service,
        incident,
        client=lambda _: '{"patch":null,"reason":"insufficient fixture evidence"}',
    )
    assert not result["proposed"]
    assert not service.db.all("SELECT * FROM jobs WHERE kind='source:activate_parser'")


def test_parser_proposals_share_the_daily_model_budget(parser_proposal, monkeypatch):
    service, incident = parser_proposal
    monkeypatch.setenv("OPS_MAX_CALLS_PER_DAY", "1")
    response = lambda _: '{"patch":null,"reason":"no evidence"}'
    ops.propose_parser(service, incident, client=response)
    with pytest.raises(ValueError, match="budget"):
        ops.propose_parser(service, incident, client=response)


@pytest.fixture
def activation(source_service, monkeypatch):
    base = (Path(jobs.__file__).parents[1] / "compat/scrape_parse.py").read_text()
    capture = json.loads(
        (Path(__file__).parents[1] / "fixtures/quantgt_sample.json").read_text()
    )
    installed = []
    monkeypatch.setattr(jobs, "install_parser_text", installed.append)

    @contextmanager
    def session():
        yield object()

    monkeypatch.setattr(jobs, "browser_session", session)
    monkeypatch.setattr(
        jobs, "capture_picks", lambda page: (copy.deepcopy(capture), [])
    )
    return source_service, base, capture, installed


def candidate_bundle(service, base, suffix="one", mutate_report=None):
    text = base.replace(
        "Return only the source", "Return only the " + suffix + " source"
    )
    assert text != base and ops.literal_only(base, text)
    root = service.settings.data_dir / "ops" / ("candidate-" + suffix)
    relative = "src/hadan/compat/scrape_parse.py"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_text(text)
    report = {
        "passed": True,
        "tests": 81,
        "files": [relative],
        "base_hashes": {relative: ops.digest(base)},
        "candidate_hashes": {relative: ops.digest(text)},
    }
    if mutate_report:
        mutate_report(report)
    (root / "verification.json").write_text(json.dumps(report))
    return root, text


def test_verified_canary_activates_without_publishing_subscriber_content(activation):
    service, base, capture, installed = activation
    candidate, text = candidate_bundle(service, base)
    result = jobs.activate_parser(service, candidate, ops.digest(text))
    assert result["activated"]
    assert installed == [text]
    path = service.settings.data_dir / "source/parser_active.py"
    assert path.read_text() == text
    assert json.loads(path.with_suffix(".json").read_text())["sha256"] == ops.digest(
        text
    )
    assert not service.db.all("SELECT * FROM events"), (
        "Canary evaluation itself must not send a content update"
    )
    assert len(service.db.all("SELECT * FROM jobs WHERE kind='source:picks'")) == 1


def test_incomplete_live_canary_never_promotes_overlay(activation, monkeypatch):
    service, base, capture, installed = activation
    candidate, text = candidate_bundle(service, base)
    broken = copy.deepcopy(capture)
    broken["weekly"]["rows"] = broken["weekly"]["rows"][:2]
    monkeypatch.setattr(jobs, "capture_picks", lambda page: (broken, []))
    with pytest.raises(RuntimeError, match="near-complete"):
        jobs.activate_parser(service, candidate, ops.digest(text))
    assert not (service.settings.data_dir / "source/parser_active.py").exists()
    assert not service.db.all("SELECT * FROM events")


def test_same_edition_mass_symbol_change_is_not_accepted_as_parser_repair(
    activation, monkeypatch
):
    service, base, capture, installed = activation
    jobs.save(service, "picks:accepted", capture)
    candidate, text = candidate_bundle(service, base)
    broken = copy.deepcopy(capture)
    for i, row in enumerate(broken["weekly"]["rows"][:3]):
        row["symbol"] = "WRONG" + str(i)
    monkeypatch.setattr(jobs, "capture_picks", lambda page: (broken, []))
    with pytest.raises(ValueError, match="too many symbols"):
        jobs.activate_parser(service, candidate, ops.digest(text))
    assert not (service.settings.data_dir / "source/parser_active.py").exists()


def test_canary_cannot_degrade_known_edition_date_to_unknown(activation, monkeypatch):
    service, base, capture, installed = activation
    jobs.save(service, "picks:accepted", capture)
    candidate, text = candidate_bundle(service, base)
    broken = copy.deepcopy(capture)
    broken["weekly"]["pick_date"] = "Unknown"
    monkeypatch.setattr(jobs, "capture_picks", lambda page: (broken, []))
    with pytest.raises((ValueError, RuntimeError)):
        jobs.activate_parser(service, candidate, ops.digest(text))
    assert not (service.settings.data_dir / "source/parser_active.py").exists()


def test_canary_rejects_duplicate_tickers_even_if_edition_date_changes(
    activation, monkeypatch
):
    service, base, capture, installed = activation
    jobs.save(service, "picks:accepted", capture)
    candidate, text = candidate_bundle(service, base)
    broken = copy.deepcopy(capture)
    broken["weekly"]["pick_date"] = "Updated on Oct 30, 2026"
    broken["weekly"]["rows"][1]["symbol"] = broken["weekly"]["rows"][0]["symbol"]
    monkeypatch.setattr(jobs, "capture_picks", lambda page: (broken, []))
    with pytest.raises((ValueError, RuntimeError)):
        jobs.activate_parser(service, candidate, ops.digest(text))
    assert not (service.settings.data_dir / "source/parser_active.py").exists()


def test_stale_verifier_base_is_rejected_before_installing_code(activation):
    service, base, capture, installed = activation
    candidate, text = candidate_bundle(
        service,
        base,
        mutate_report=lambda report: report["base_hashes"].update(
            {"src/hadan/compat/scrape_parse.py": "0" * 64}
        ),
    )
    with pytest.raises(ValueError, match="mismatch"):
        jobs.activate_parser(service, candidate, ops.digest(text))
    assert not installed


def test_rollback_restores_prior_verified_overlay_and_metadata(activation):
    service, base, capture, installed = activation
    first, first_text = candidate_bundle(service, base, "one")
    second, second_text = candidate_bundle(service, base, "two")
    jobs.activate_parser(service, first, ops.digest(first_text))
    jobs.activate_parser(service, second, ops.digest(second_text))
    assert jobs.rollback_parser(service)
    path = service.settings.data_dir / "source/parser_active.py"
    assert path.read_text() == first_text
    assert jobs.load_active_parser(service)
    assert installed[-1] == first_text
    assert service.db.one("SELECT id FROM audit WHERE action='parser_rolled_back'")


def test_first_overlay_rollback_returns_to_packaged_parser(activation):
    service, base, capture, installed = activation
    candidate, text = candidate_bundle(service, base)
    jobs.activate_parser(service, candidate, ops.digest(text))
    assert jobs.rollback_parser(service)
    assert not jobs.load_active_parser(service)


def test_mismatched_overlay_files_cannot_permanently_block_source_startup(
    activation, monkeypatch
):
    service, base, capture, installed = activation
    candidate, text = candidate_bundle(service, base)
    jobs.activate_parser(service, candidate, ops.digest(text))
    path = service.settings.data_dir / "source/parser_active.py"
    path.with_suffix(".json").write_text(
        json.dumps({"base_sha256": ops.digest(base), "sha256": "0" * 64})
    )
    monkeypatch.setattr("dotenv.load_dotenv", lambda **_: None)
    monkeypatch.setattr(jobs, "Service", lambda settings: service)
    monkeypatch.setattr(jobs, "picks", lambda _: {"fixture": True})
    monkeypatch.setattr(jobs.sys, "argv", ["hadan.sources.jobs", "picks"])
    try:
        jobs.main()
    except SystemExit as exc:
        assert exc.code == 1
    assert not path.exists(), (
        "Hash mismatch after interrupted promotion must restore packaged parser, not fail on every restart"
    )


def test_source_hard_timeout_rolls_back_active_overlay(activation, monkeypatch):
    service, base, capture, installed = activation
    candidate, text = candidate_bundle(service, base)
    jobs.activate_parser(service, candidate, ops.digest(text))

    class TimedOut:
        pid = 12345

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("fixture-source", timeout)
            return b"", b""

    monkeypatch.setattr("hadan.worker.subprocess.Popen", lambda *_, **__: TimedOut())
    monkeypatch.setattr("hadan.worker.os.killpg", lambda *args: None)
    with pytest.raises(DeliveryError, match="time limit"):
        Worker(service, queue="source").dispatch(
            {"kind": "source:picks", "payload": "{}"}
        )
    assert not (service.settings.data_dir / "source/parser_active.py").exists(), (
        "SIGKILL cannot rely on child finally/except to rollback"
    )
