"""[NFR-01..NFR-12, NFR-99] TEST_SPEC "Deferred to Downstream Phases" rows (static and unit layer).

Rows: 02-architecture/TEST_SPEC.md, "Deferred to Downstream Phases" #1-#33. Suite-level criteria (mutation score,
VERIFIED status) READ the harness evidence under .methodology/ and never run pytest over this directory.
"""
import ast
import asyncio
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from importlib import metadata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from packaging.requirements import Requirement
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from taskq_api.app import create_app
from taskq_api.models.api_key import ApiKey
from taskq_api.models.base import Base
from taskq_api.models.task import Task
from taskq_api.models.task_result import TaskResult
from taskq_api.repository.session import session_scope
from taskq_api.service import auth, executor
from taskq_api.service.redaction import redact

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "03-development" / "src"
TESTS = ROOT / "03-development" / "tests"
BIN = Path(sys.executable).parent
ALLOWED_LICENSES = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF"}


def _py_files(base=SRC):
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path):
    return ast.parse(path.read_text(), filename=str(path))


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path):
    out = {}
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            out[_norm(re.split(r"[=<>!~ ]", line)[0])] = line
    return out


# ---------------------------------------------------------------- NFR-01

def test_nfr01_benchmarks_use_pytest_benchmark():
    benchmark_modules_min, hand_rolled_timer_benchmarks = "1", "0"
    using, hand_rolled = set(), []
    for path in _py_files(TESTS):
        for fn in (n for n in ast.walk(_parse(path)) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")):
            if any(a.arg == "benchmark" for a in fn.args.args):
                using.add(path)
            elif "p95" in fn.name:  # a latency-percentile test that does not measure through pytest-benchmark
                hand_rolled.append(f"{path.name}::{fn.name}")
    assert len(using) >= int(benchmark_modules_min)
    assert len(hand_rolled) == int(hand_rolled_timer_benchmarks), hand_rolled


# ---------------------------------------------------------------- NFR-02

def test_nfr02_no_shell_true_eval_exec_in_source():
    grep_hits = "0"
    hits = []
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                hits.append(f"{path.name}:{node.lineno} {node.func.id}(")
            if isinstance(node, ast.keyword) and node.arg == "shell" and getattr(node.value, "value", None) is True:
                hits.append(f"{path.name}:{node.value.lineno} shell=True")
    assert len(hits) == int(grep_hits), hits


_SQL = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", re.I)


def _leading_text(node):
    while isinstance(node, ast.BinOp):
        node = node.left
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return str(node.values[0].value)
    return ""


def _builds_sql(node):
    """True for an f-string with substitutions, a `+`/`%` expression or a `.format()` call that assembles SQL text."""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values) and bool(_SQL.match(_leading_text(node)))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return any(_SQL.match(_leading_text(side)) for side in (node, node.right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return bool(_SQL.match(_leading_text(node.func.value)))
    return False


def test_nfr02_no_string_concatenated_sql_in_source():
    sql_concat_hits = "0"
    hits = [f"{path.name}:{node.lineno}" for path in _py_files() for node in ast.walk(_parse(path)) if _builds_sql(node)]
    assert len(hits) == int(sql_concat_hits), hits


def test_nfr02_api_key_hashed_and_compared_constant_time(tmp_path, monkeypatch):
    hash_hex_length, compare_digest_calls_min = "64", "1"
    plaintext = "tq-nfr02-plaintext"
    url = f"sqlite:///{tmp_path / 'nfr02.db'}"
    monkeypatch.setenv("TASKQ_DB_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ApiKey(id=str(uuid.uuid4()), key_hash=auth.hash_key(plaintext), scope="read"))
        s.commit()
    engine.dispose()
    calls = []
    real = auth.hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: (calls.append((a, b)), real(a, b))[1])
    with TestClient(create_app()) as c:
        r = c.get("/v1/tasks/00000000-0000-4000-8000-000000000000", headers={"X-API-Key": plaintext})
    digest = auth.hash_key(plaintext)
    assert r.status_code == 404  # authenticated, then not found
    assert len(digest) == int(hash_hex_length) and digest == hashlib.sha256(plaintext.encode()).hexdigest()
    assert plaintext not in digest
    assert len(calls) >= int(compare_digest_calls_min) and calls[0] == (digest, digest)


def test_nfr02_bandit_reports_zero_high_and_zero_medium():
    bandit_high, bandit_medium = "0", "0"
    proc = subprocess.run([str(BIN / "bandit"), "-r", str(SRC), "-f", "json", "-q"],
                          capture_output=True, text=True, timeout=7)
    report = json.loads(proc.stdout)
    severities = [r["issue_severity"] for r in report["results"]]
    assert report["metrics"]["_totals"]["loc"] > 0
    assert severities.count("HIGH") == int(bandit_high)
    assert severities.count("MEDIUM") == int(bandit_medium)


# ---------------------------------------------------------------- NFR-03

def test_nfr03_transaction_commit_on_success_rollback_on_exception(tmp_path, monkeypatch):
    commits_on_success, rollbacks_on_exception = "1", "1"
    engine = create_engine(f"sqlite:///{tmp_path / 'tx.db'}")
    Base.metadata.create_all(engine)
    counts = {"commit": 0, "rollback": 0, "close": 0}
    for name in counts:
        real = getattr(Session, name)
        monkeypatch.setattr(Session, name, lambda self, _r=real, _n=name: (counts.__setitem__(_n, counts[_n] + 1), _r(self))[1])
    with session_scope(engine) as s:
        s.add(Task(id="ok", command="echo hi", name="kept"))
    assert (counts["commit"], counts["rollback"]) == (int(commits_on_success), 0)
    with pytest.raises(ValueError):
        with session_scope(engine) as s:
            s.add(Task(id="bad", command="echo hi", name="dropped"))
            s.flush()
            raise ValueError("boom")
    assert (counts["commit"], counts["rollback"]) == (int(commits_on_success), int(rollbacks_on_exception))
    assert counts["close"] == 2  # the connection is released on both paths
    with Session(engine) as s:
        assert {t.id for t in s.query(Task)} == {"ok"}
    engine.dispose()


def _handler_names(handler):
    if handler.type is None:
        return set()
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", "") for t in types}


def test_nfr03_no_bare_except_or_except_exception_pass():
    bare_except_hits, except_exception_pass_hits = "0", "0"
    bare, swallowed = [], []
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                bare.append(f"{path.name}:{node.lineno}")
            if _handler_names(node) & {"Exception", "BaseException"} and all(isinstance(b, ast.Pass) for b in node.body):
                swallowed.append(f"{path.name}:{node.lineno}")
    assert len(bare) == int(bare_except_hits), bare
    assert len(swallowed) == int(except_exception_pass_hits), swallowed


def test_nfr03_cancelled_error_is_reraised(tmp_path):
    swallowed_cancellations = "0"
    engine = create_engine(f"sqlite:///{tmp_path / 'cancel.db'}")
    Base.metadata.create_all(engine)
    run_id = str(uuid.uuid4())
    with Session(engine) as s:
        task = Task(command="sleep 30", name="cancel-me")
        s.add(task)
        s.flush()
        s.add(TaskResult(id=run_id, task_id=task.id, status="pending"))
        s.commit()

    async def scenario():
        job = asyncio.create_task(executor.execute(engine, run_id, "sleep 30", 30.0))
        await asyncio.sleep(0.3)
        job.cancel()
        try:
            await job
        except asyncio.CancelledError:
            return 0
        return 1

    result_swallowed = asyncio.run(scenario())
    engine.dispose()
    handlers = [(p.name, n) for p in _py_files() for n in ast.walk(_parse(p))
                if isinstance(n, ast.ExceptHandler) and "CancelledError" in _handler_names(n)]
    not_reraised = [f"{name}:{n.lineno}" for name, n in handlers if not any(isinstance(x, ast.Raise) for x in ast.walk(n))]
    assert result_swallowed == int(swallowed_cancellations)
    assert handlers and not not_reraised, not_reraised


# ---------------------------------------------------------------- NFR-04

def test_nfr04_sensitive_lines_replaced_with_redacted():
    min_secret_length, unredacted_matches = "8", "0"
    pattern = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+|postgres(ql)?://[^\s]+)")
    secret = "A" * int(min_secret_length)
    sensitive = [f"key sk-{secret}", f"auth token=abc123 end", f"Authorization: Bearer {secret}",
                 "dsn postgresql://taskq:s3cretPassw0rd@db.internal/taskq", "dsn postgres://u:pw@host/db"]
    clean = ["plain output line", "sk-short is too short"]
    text = "\n".join([sensitive[0], clean[0], *sensitive[1:], clean[1]])
    out = redact(text).split("\n")
    result_unredacted_matches = sum(1 for line in out if pattern.search(line))
    assert out[0] == "[REDACTED]" and out[2:6] == ["[REDACTED]"] * 4  # the whole line, not a substring
    assert [out[1], out[6]] == clean
    assert result_unredacted_matches == int(unredacted_matches)
    assert not pattern.search(executor._tail(f"sk-{secret}\n".encode()))  # the run-output path uses the same filter


# ---------------------------------------------------------------- NFR-05

def _public_defs(path):
    tree = _parse(path)
    stack = [(tree, True)]
    while stack:
        node, public = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_public = public and not child.name.startswith("_")
                if is_public:
                    yield child
                if isinstance(child, ast.ClassDef):  # methods of a public class; nested functions are not public API
                    stack.append((child, is_public))


def test_nfr05_public_docstrings_reference_fr_or_nfr():
    docstring_coverage_pct = "100"
    ref = re.compile(r"\[(?:N?FR-\d+)")
    total, missing = 0, []
    for path in _py_files():
        for node in _public_defs(path):
            total += 1
            if not ref.search(ast.get_docstring(node) or ""):
                missing.append(f"{path.relative_to(SRC)}:{node.lineno} {node.name}")
    result_pct = 100.0 * (total - len(missing)) / total
    assert total > 0
    assert result_pct == float(docstring_coverage_pct), f"{result_pct:.1f}% missing: {missing}"


# ---------------------------------------------------------------- NFR-06

def _importlinter():
    parser = configparser.ConfigParser()
    parser.read(ROOT / ".importlinter")
    contracts = {s.split(":", 2)[2]: dict(parser[s]) for s in parser.sections() if s.startswith("importlinter:contract:")}
    return parser, contracts


def _lines(value):
    return [v.strip() for v in value.splitlines() if v.strip()]


def test_nfr06_importlinter_declares_layers_and_independence_contracts():
    layer_count, independence_modules = "4", "2"
    assert (ROOT / ".importlinter").is_file()
    _, contracts = _importlinter()
    layers = [c for c in contracts.values() if c["type"] == "layers"]
    independence = [c for c in contracts.values() if c["type"] == "independence"]
    assert len(layers) >= 1 and _lines(layers[0]["layers"]) == [
        "taskq_api.api", "taskq_api.service", "taskq_api.repository", "taskq_api.models"]
    assert len(_lines(layers[0]["layers"])) == int(layer_count)
    leaves = [set(_lines(c["modules"])) for c in independence]
    assert {"taskq_api.config", "taskq_api.errors"} in leaves
    assert len(_lines(independence[leaves.index({"taskq_api.config", "taskq_api.errors"})]["modules"])) == int(independence_modules)


def _lint_imports(cwd, pythonpath):
    env = {**os.environ, "PYTHONPATH": str(pythonpath)}
    return subprocess.run([str(BIN / "lint-imports"), "--config", str(ROOT / ".importlinter"), "--no-cache"],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=7)


def test_nfr06_sqlalchemy_forbidden_outside_repository_rejects_seeded_violation(tmp_path):
    seeded_violations, expected_nonzero_exit = "1", "1"
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "taskq_api" / "service" / "seeded_violation.py").write_text("import sqlalchemy  # seeded violation\n")
    proc = _lint_imports(tmp_path, copy)
    assert int(seeded_violations) == 1
    assert proc.returncode == int(expected_nonzero_exit), proc.stdout
    report = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
    assert "SQLAlchemy only in repository and models BROKEN" in report
    assert "taskq_api.service.seeded_violation -> sqlalchemy" in report


def test_nfr06_lint_imports_exits_zero():
    expected_exit_code = "0"
    proc = _lint_imports(ROOT, SRC)
    assert proc.returncode == int(expected_exit_code), proc.stdout + proc.stderr
    assert "BROKEN" not in proc.stdout


def test_nfr06_no_wildcard_ignore_imports_or_downgraded_contracts():
    wildcard_ignore_hits, downgraded_contracts = "0", "0"
    _, contracts = _importlinter()
    wildcards = [(n, v) for n, c in contracts.items() for v in _lines(c.get("ignore_imports", "")) if "*" in v]
    downgraded = [n for n, c in contracts.items()
                  if c.get("unmatched_ignore_imports_alerting", "error") != "error" or c["type"] not in
                  {"layers", "independence", "forbidden"}]
    forbidden = [c for c in contracts.values() if c["type"] == "forbidden" and "sqlalchemy" in _lines(c["forbidden_modules"])]
    assert len(wildcards) == int(wildcard_ignore_hits), wildcards
    assert len(downgraded) == int(downgraded_contracts), downgraded
    assert forbidden and "taskq_api.service" in _lines(forbidden[0]["source_modules"])
    assert "taskq_api.api" in _lines(forbidden[0]["source_modules"])


# ---------------------------------------------------------------- NFR-07

def _lock():
    return _pins(ROOT / "requirements.lock")


def _closure(names):
    seen, todo = {}, list(names)
    while todo:
        name = _norm(todo.pop())
        if name in seen:
            continue
        dist = metadata.distribution(name)
        seen[name] = dist.version
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(req.name)
    return seen


def test_nfr07_runtime_deps_pinned_and_lock_file_complete():
    unpinned_deps, lock_missing_transitive = "0", "0"
    direct, lock = _pins(ROOT / "requirements.txt"), _lock()
    unpinned = [n for n, line in direct.items() if not re.fullmatch(r"[\w.-]+==[\w.]+", line)]
    unpinned += [n for n, line in lock.items() if "==" not in line]
    closure = _closure(direct)
    missing = [n for n, v in closure.items() if lock.get(n, "").split("==")[-1].strip() != v]
    assert direct and len(unpinned) == int(unpinned_deps), unpinned
    assert len(missing) == int(lock_missing_transitive), missing


def _sbom():
    return json.loads((ROOT / "08-config" / "SBOM.json").read_text())["components"]


def _sbom_license(component):
    ids = {lic["license"].get("id") or lic["license"].get("name") for lic in component.get("licenses", [])}
    return {"PSF" if i == "PSF-2.0" else i for i in ids}  # the PSF-2.0 SPDX id is the allowlist's "PSF" entry


def test_nfr07_all_dependency_licenses_in_allowlist():
    disallowed_licenses, allowlist_size = "0", "5"
    components = _sbom()
    disallowed = [c["name"] for c in components if not _sbom_license(c) <= ALLOWED_LICENSES or not _sbom_license(c)]
    keywords = ("mit", "bsd", "apache", "python software foundation", "psf")
    proc = subprocess.run([str(BIN / "pip-licenses"), "--format=json", "--from=mixed", "--with-system"],
                          capture_output=True, text=True, timeout=7)
    installed = {_norm(p["Name"]): p["License"] for p in json.loads(proc.stdout)}
    scanned = [(n, installed[n]) for n in _lock() if n in installed]
    outside = [(n, lic) for n, lic in scanned if not any(k in lic.lower() for k in keywords)]
    assert len(ALLOWED_LICENSES) == int(allowlist_size)
    assert len(disallowed) == int(disallowed_licenses), disallowed
    assert len(scanned) == len(_lock()) and not outside, outside


def test_nfr07_license_scan_covers_full_dependency_tree():
    uncovered_transitive_deps = "0"
    covered = {_norm(c["name"]) for c in _sbom()}
    tree = set(_closure(_pins(ROOT / "requirements.txt")))
    uncovered = sorted(tree - covered)
    assert len(uncovered) == int(uncovered_transitive_deps), uncovered
    assert covered <= set(_lock())


def test_nfr07_sbom_json_has_name_version_license_and_direct_transitive():
    sbom_required_fields, sbom_entries_missing_fields = "4", "0"
    required = ("name", "version", "license", "scope")
    direct = set(_pins(ROOT / "requirements.txt"))
    missing = []
    for c in _sbom():
        scope = c.get("scope") or {p["name"]: p["value"] for p in c.get("properties", [])}.get("taskq:dependency-type")
        present = {"name": c.get("name"), "version": c.get("version"), "license": _sbom_license(c), "scope": scope}
        expected_scope = "direct" if _norm(c.get("name", "")) in direct else "transitive"
        if not all(present[f] for f in required) or str(scope).lower() not in {"direct", "transitive", "required"} \
                or (scope in ("direct", "transitive") and scope != expected_scope):
            missing.append(c.get("name"))
    assert len(required) == int(sbom_required_fields)
    assert len(missing) == int(sbom_entries_missing_fields), missing


# ---------------------------------------------------------------- NFR-08

def _harness_config():
    return json.loads((ROOT / ".methodology" / "harness_config.json").read_text())


def test_nfr08_harness_config_enables_mutation_testing():
    mutation_testing_enabled = "1"
    assert _harness_config()["features"]["mutation_testing"] is bool(int(mutation_testing_enabled))


def test_nfr08_mutation_score_at_least_70():
    mutation_score_floor = "70"
    evidence = json.loads((ROOT / ".methodology" / "mutation_score.json").read_text())
    assert evidence["tool"] == "mutmut" and evidence["killed"] + evidence["survived"] > 0
    assert evidence["score"] >= int(mutation_score_floor)


def test_nfr08_mutation_scope_limited_to_service_and_repository_with_rationale():
    scoped_layers, rationale_entries_min = "2", "1"
    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "setup.cfg")
    paths = [p.strip() for p in cfg["mutmut"]["paths_to_mutate"].split(",")]
    config = _harness_config()
    rationale = [v for k, v in config.items() if "mutation" in k and "note" in k and isinstance(v, str) and v.strip()]
    assert sorted(Path(p).name for p in paths) == ["repository", "service"]
    assert len(paths) == int(scoped_layers)
    assert len(rationale) >= int(rationale_entries_min)
    assert "service/" in rationale[0] and "repository/" in rationale[0]


# ---------------------------------------------------------------- NFR-09

def _test_functions(path):
    return [n for n in ast.walk(_parse(path)) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([getattr(node, "id", ""), *reversed(parts)]).strip(".")


def test_nfr09_no_skip_skipif_xfail_or_stub_tests():
    skip_markers, xfail_markers, stub_tests = "0", "0", "0"
    skips, xfails, stubs = [], [], []
    for path in _py_files(TESTS):
        tree = _parse(path)
        for node in ast.walk(tree):
            name = _dotted(node.func if isinstance(node, ast.Call) else node) if isinstance(node, (ast.Call, ast.Attribute)) else ""
            if name.startswith("pytest.") and any(name.endswith(s) for s in (".skip", ".skipif", "importorskip")):
                skips.append(f"{path.name}:{node.lineno}")
            if name.startswith("pytest.") and name.endswith(".xfail") or name == "pytest.xfail":
                xfails.append(f"{path.name}:{node.lineno}")
        for fn in _test_functions(path):
            body = [b for b in fn.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
            trivial = not body or all(isinstance(b, ast.Pass) for b in body) or all(
                isinstance(b, ast.Assert) and isinstance(b.test, ast.Constant) and b.test.value is True for b in body)
            if trivial:
                stubs.append(f"{path.name}::{fn.name}")
    assert len(skips) == int(skip_markers), skips
    assert len(xfails) == int(xfail_markers), xfails
    assert len(stubs) == int(stub_tests), stubs


def test_nfr09_every_test_function_has_assert():
    zero_assert_tests = "0"
    zero = []
    for path in _py_files(TESTS):
        for fn in _test_functions(path):
            nodes = list(ast.walk(fn))
            asserts = any(isinstance(n, ast.Assert) for n in nodes) or any(
                isinstance(n, ast.Call) and _dotted(n.func).split(".")[-1].lstrip("_").startswith("assert") for n in nodes) or any(
                isinstance(n, ast.With) and any(_dotted(getattr(i.context_expr, "func", i.context_expr)) == "pytest.raises"
                                                for i in n.items) for n in nodes)
            if not asserts:
                zero.append(f"{path.name}::{fn.name}")
    assert len(zero) == int(zero_assert_tests), zero


def test_nfr09_no_test_exclusion_flags_or_config():
    exclusion_flags = "0"
    flag = re.compile(r"(--ignore\b|--ignore-glob|--deselect|(?<![\w-])-k\s|collect_ignore)")
    configs = [ROOT / "Makefile", ROOT / "setup.cfg", ROOT / "alembic.ini", *ROOT.glob("pytest.ini"), *ROOT.glob("tox.ini"),
               *ROOT.glob("pyproject.toml"), *(ROOT / ".github" / "workflows").glob("*.yml"), TESTS / "conftest.py"]
    hits = []
    for cfg in (c for c in configs if c.is_file()):
        for no, line in enumerate(cfg.read_text().splitlines(), 1):
            code = line.split("#")[0]
            if flag.search(code) and (cfg.name != "harness_quality_gate.yml" or "pytest" in code):
                hits.append(f"{cfg.name}:{no}")
    setup = configparser.ConfigParser()
    setup.read(ROOT / "setup.cfg")
    assert not setup.has_option("tool:pytest", "testpaths")  # no directory dropped from collection
    assert len(hits) == int(exclusion_flags), hits


def test_nfr09_verified_status_only_after_passing_run():
    verified_without_passing_run = "0"
    matrix = (ROOT / "01-requirements" / "TRACEABILITY_MATRIX.md").read_text()
    evidence = (ROOT / ".methodology" / "gate_evidence" / "gate2" / "test_coverage.txt").read_text()
    passing = {m.group(1) for m in re.finditer(r"^(\S+\.py) (\.+)\s+\[\s*\d+%\]$", evidence, re.M)}
    summary = re.search(r"(\d+) passed(?:, \d+ warnings?)? in", evidence)
    assert summary and not re.search(r"\b(failed|error|skipped|xfailed)\b", evidence.splitlines()[-1])
    unproven = []
    for row in re.finditer(r"^\| (FR-\d+) \| verified \|.*?\| (03-development/tests/\S+?)(?:, |\s*\|)", matrix, re.M | re.I):
        if row.group(2) not in passing:
            unproven.append(row.group(1))
    tracking = (ROOT / "01-requirements" / "SPEC_TRACKING.md").read_text()
    for row in re.finditer(r"^\| (FR-\d+) \|.*\| VERIFIED \|", tracking, re.M):
        if not re.search(rf"^\| {row.group(1)} \| verified \|", matrix, re.M | re.I):
            unproven.append(row.group(1))
    assert re.search(r"^\| FR-01 \| verified \|", matrix, re.M | re.I)
    assert len(unproven) == int(verified_without_passing_run), unproven


# ---------------------------------------------------------------- NFR-10

INTEGRATION = TESTS / "integration"


def test_nfr10_integration_tests_use_asgi_transport_not_handlers():
    direct_handler_calls = "0"
    modules = _py_files(INTEGRATION)
    handlers, calls, missing = set(), [], []
    for path in modules:
        tree = _parse(path)
        text = path.read_text()
        if "ASGITransport" not in text or "AsyncClient" not in text or "TestClient" in text:
            missing.append(path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("taskq_api.api.routes_"):
                handlers |= {a.asname or a.name for a in node.names}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in handlers:
                calls.append(f"{path.name}:{node.lineno}")
    assert modules and not missing, missing
    assert len(calls) == int(direct_handler_calls), calls


def test_nfr10_integration_suite_covers_required_scenarios():
    required_error_codes, other_required_scenarios = "7", "4"
    codes = {401, 403, 404, 409, 422, 429, 503}
    seen, funcs = set(), {}
    for path in _py_files(INTEGRATION):
        tree = _parse(path)
        seen |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, int)}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test_"):
                funcs[fn.name] = fn

    def attrs(fn):
        return {n.attr.lower() for n in ast.walk(fn) if isinstance(n, ast.Attribute)}

    def consts(fn):
        return {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)}

    groups = {
        "crud": [n for n, f in funcs.items() if "crud" in n and {"post", "get", "delete"} <= attrs(f)],
        "migration": [n for n, f in funcs.items() if "migration" in n and {"upgrade", "downgrade"} <= attrs(f)],
        "rate_limit": [n for n, f in funcs.items() if "rate_limit" in n and {429, 200} <= consts(f)],
        "drain": [n for n, f in funcs.items() if "drain" in n and "interrupted" in consts(f)],
    }
    assert len(codes) == int(required_error_codes) and codes <= seen, codes - seen
    assert len(groups) == int(other_required_scenarios) and all(groups.values()), [k for k, v in groups.items() if not v]


# ---------------------------------------------------------------- NFR-11

def test_nfr11_maintainability_index_at_least_80():
    from radon.metrics import mi_visit
    from radon.raw import analyze

    mi_floor = "80"
    weighted, lloc_total = 0.0, 0
    for path in _py_files():
        text = path.read_text()
        lloc = analyze(text).lloc
        weighted += mi_visit(text, multi=True) * lloc
        lloc_total += lloc
    assert lloc_total > 0
    assert weighted / lloc_total >= int(mi_floor), weighted / lloc_total


def test_nfr11_cyclomatic_complexity_at_most_10():
    from radon.complexity import cc_visit

    cc_ceiling = "10"
    over = []
    for path in _py_files():
        for block in cc_visit(path.read_text()):
            for item in [block, *getattr(block, "methods", [])]:
                if item.complexity > int(cc_ceiling):
                    over.append(f"{path.name}:{item.name}={item.complexity}")
    assert not over, over


def test_nfr11_file_and_directory_size_limits():
    max_file_lines, max_dir_files = "400", "15"
    long_files = [f"{p.name}={len(p.read_text().splitlines())}" for p in _py_files()
                  if len(p.read_text().splitlines()) > int(max_file_lines)]
    crowded = [f"{d.name}={n}" for d in {p.parent for p in _py_files()}
               if (n := len([p for p in d.iterdir() if p.is_file() and p.suffix == ".py"])) > int(max_dir_files)]
    assert not long_files, long_files
    assert not crowded, crowded


def test_nfr11_api_handlers_at_most_40_lines():
    max_handler_lines = "40"
    handlers = []
    for path in sorted((SRC / "taskq_api" / "api").glob("routes_*.py")):
        for fn in ast.walk(_parse(path)):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.decorator_list:
                handlers.append((f"{path.name}:{fn.name}", fn.end_lineno - fn.decorator_list[0].lineno + 1))
    assert len(handlers) >= 9
    assert [h for h in handlers if h[1] > int(max_handler_lines)] == []


# ---------------------------------------------------------------- NFR-12 / NFR-99

def test_nfr12_verify_system_target_chains_required_steps():
    required_steps = "4"
    makefile = (ROOT / "Makefile").read_text()
    recipe = makefile.split("verify-system:\n", 1)[1]
    steps = [
        ("upgrade head", recipe.find("alembic -c alembic.ini upgrade head")),
        ("full suite", recipe.find("pytest 03-development/tests")),
        ("smoke healthz", recipe.find("/healthz")),
        ("smoke readyz", recipe.find("/readyz")),
        ("downgrade base", recipe.find("alembic -c alembic.ini downgrade base")),
        ("upgrade head again", recipe.rfind("alembic -c alembic.ini upgrade head")),
    ]
    positions = [pos for _, pos in steps]
    assert "uvicorn" in recipe and len(steps) - 2 == int(required_steps)
    assert all(p >= 0 for p in positions), steps
    assert positions == sorted(positions) and len(set(positions)) == len(positions), steps
    assert "verify-system: PASS" in recipe


def test_nfr99_open_issues_recorded_for_stakeholder_confirmation():
    open_items = "7"
    srs = (ROOT / "01-requirements" / "SRS.md").read_text()
    items = re.findall(r"^(\d+)\. \*\*NFR-99-(\d+)", srs, re.M)
    assert [n for n, _ in items] == [m for _, m in items]  # numbered list matches the NFR-99-N ids
    assert sorted(int(m) for _, m in items) == list(range(1, int(open_items) + 1))
    assert "stakeholder" in srs[srs.index("NFR-99: Ambiguity resolution"):].lower()
