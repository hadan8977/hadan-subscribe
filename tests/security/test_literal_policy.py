import pytest

from hadan.ops import literal_only


def test_parser_literal_policy_allows_only_existing_text_constants():
    original = 'import re\nLABEL = "Portfolio"\ndef parse(text):\n    return re.sub(r"old\\s+", "", text)\n'
    candidate = 'import re\nLABEL = "Positions"\ndef parse(text):\n    return re.sub(r"new\\s+", "", text)\n'
    assert literal_only(original, candidate) is True


@pytest.mark.parametrize(
    "candidate",
    [
        'import os\nvalue = "a"\n',
        'value = "a"\n__import__("os").system("id")\n',
        'value = b"a"\n',
        'value = f"{__import__("os")}"\n',
        'value = "a"\nraise SystemExit(0)\n',
        'value = "a"\nexec("print(1)")\n',
        'value = "a"\nvalue = "b"\n',
        'value = lambda: "a"\n',
    ],
)
def test_parser_literal_policy_rejects_new_executable_structure(candidate):
    assert literal_only('value = "a"\n', candidate) is False


def test_parser_literal_policy_does_not_normalize_numeric_or_boolean_constants():
    assert literal_only("enabled = True\n", "enabled = False\n") is False
    assert literal_only("limit = 50\n", "limit = 50000\n") is False


@pytest.mark.parametrize("prefix", ["", "#" + "padding" * 50 + "\n"])
def test_parser_literal_policy_rejects_encoding_cookie_bytecode_bypass(prefix):
    original = 'value = "safe"\n'
    candidate = (
        prefix + "# coding: unicode_escape\n" + original + "#\\u000ainjected = True\n"
    )
    # ast.parse(str) treats the final line as a comment, but a Python loader
    # compiling bytes would decode the escape and execute an extra assignment.
    namespace = {}
    exec(compile(candidate.encode("utf-8"), "fixture.py", "exec"), namespace)
    assert namespace["injected"] is True
    assert literal_only(original, candidate) is False


@pytest.mark.parametrize(
    "defect",
    [
        "expected_hash",
        "candidate_hash",
        "base_hash",
        "failed_tests",
        "missing_tests",
        "protected_file",
        "added_import",
    ],
)
def test_source_activation_independently_rejects_tampered_approval(
    harness, monkeypatch, defect
):
    import json
    from pathlib import Path

    from hadan.security import digest
    from hadan.sources import jobs

    _, service, _ = harness
    relative = "src/hadan/compat/scrape_parse.py"
    base = (Path(jobs.__file__).parents[1] / "compat/scrape_parse.py").read_text(
        encoding="utf-8"
    )
    text = base + "\n# verified candidate fixture\n"
    if defect == "added_import":
        text += "import os\n"
    candidate = service.settings.data_dir / "ops/candidate-fixture"
    path = candidate / relative
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    report = {
        "passed": True,
        "tests": 100,
        "files": [relative],
        "base_hashes": {relative: digest(base)},
        "candidate_hashes": {relative: digest(text)},
    }
    expected = digest(text)
    if defect == "expected_hash":
        expected = "0" * 64
    elif defect == "candidate_hash":
        report["candidate_hashes"][relative] = "0" * 64
    elif defect == "base_hash":
        report["base_hashes"][relative] = "0" * 64
    elif defect == "failed_tests":
        report["passed"] = False
    elif defect == "missing_tests":
        report["tests"] = 0
    elif defect == "protected_file":
        report["files"].append("src/hadan/security.py")
    (candidate / "verification.json").write_text(json.dumps(report))

    def forbidden_network():
        raise AssertionError("unapproved candidate reached live source verification")

    monkeypatch.setattr(jobs, "browser_session", forbidden_network)
    with pytest.raises(ValueError):
        jobs.activate_parser(service, str(candidate), expected)
    assert not (service.settings.data_dir / "source/parser_active.py").exists()
