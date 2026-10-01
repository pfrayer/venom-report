"""Fixtures are real venom v1.3.0 outputs of tests/fixtures/suite_a.yml:
xml and json formats with -vvv, and xml without dumps. `auth.token` is fake."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest

from venom_report import cli as vr

FIXTURES = Path(__file__).parent / "fixtures"


def copy(tmp_path: Path, name: str, age: float = 0.0) -> Path:
    """git does not keep mtimes: stamp the dumps, then the report, as venom would."""
    out = tmp_path / name
    shutil.copytree(FIXTURES / name, out)
    now = time.time() - age
    for p in out.iterdir():
        stamp = now if p.name.startswith("test_results") else now - 0.1
        os.utime(p, (stamp, stamp))
    return out


def run(out: Path, *args: str) -> dict:
    assert vr.main([str(out), *args]) == 0
    html = (out / "report" / "index.html").read_text()
    m = re.search(r'id="data">(.*?)</script>', html, re.DOTALL)
    assert m
    return json.loads(m.group(1))


def tcs(data: dict) -> dict[str, dict]:
    return {tc["name"]: tc for s in data["suites"] for tc in s["testcases"]}


@pytest.mark.parametrize("fmt", ["xml", "json"])
def test_statuses(tmp_path: Path, fmt: str) -> None:
    t = tcs(run(copy(tmp_path, fmt)))
    assert {k: v["status"] for k, v in t.items()} == {
        "TC1-exec-ok": "pass",
        "TC2-exec-fails": "fail",
        "TC3-http": "fail",
        "TC4-skipped": "skip",
        "TC5-loop": "fail",
    }
    assert "expected: 2" in t["TC4-skipped"]["skipReason"]


@pytest.mark.parametrize("fmt", ["xml", "json"])
def test_failing_step_and_assertion_are_located(tmp_path: Path, fmt: str) -> None:
    tc = tcs(run(copy(tmp_path, fmt)))["TC2-exec-fails"]
    s1, s2 = tc["steps"]
    assert (s1["status"], s2["status"]) == ("pass", "fail")
    assert s2["type"] == "exec" and s2["systemout"] == "two"
    assert [a["ok"] for a in s2["assertions"]] == [False, True]


@pytest.mark.parametrize("fmt", ["xml", "json"])
def test_ranged_steps_are_distinct(tmp_path: Path, fmt: str) -> None:
    steps = tcs(run(copy(tmp_path, fmt)))["TC5-loop"]["steps"]
    assert [(s["n"], s["r"], s["status"]) for s in steps] == [
        (1, 0, "pass"),
        (1, 1, "fail"),
    ]


def test_http_step(tmp_path: Path) -> None:
    (step,) = tcs(run(copy(tmp_path, "xml")))["TC3-http"]["steps"]
    http = step["http"]
    assert (http["method"], http["url"], http["status"]) == (
        "POST",
        "http://127.0.0.1:18765/x?a=1",
        501,
    )
    assert http["reqBody"] == '{\n  "k": "v"\n}'
    assert step["status"] == "fail" and step["assertions"] == [
        {"text": "result.statuscode ShouldEqual 200", "ok": False}
    ]


@pytest.mark.parametrize("fmt", ["xml", "json"])
def test_secrets_never_reach_the_report(tmp_path: Path, fmt: str) -> None:
    out = copy(tmp_path, fmt)
    run(out)
    html = (out / "report" / "index.html").read_text()
    assert "SECRET123" not in html


def test_custom_redact_pattern(tmp_path: Path) -> None:
    out = copy(tmp_path, "xml")
    data = run(out, "--redact", "content-type")
    (step,) = tcs(data)["TC3-http"]["steps"]
    assert step["http"]["reqHeaders"]["Content-Type"] == "***"


def test_stale_dumps_are_ignored(tmp_path: Path) -> None:
    out = copy(tmp_path, "xml")
    old = time.time() - 86400
    for p in out.glob("suite_a.TC2-*.dump.json"):
        os.utime(p, (old, old))
    tc = tcs(run(out))["TC2-exec-fails"]
    # step 2 is still listed (the failure names it), but without its stale dump
    assert [(s["n"], s["hasDump"]) for s in tc["steps"]] == [(2, False)]


def test_no_dumps_falls_back_on_junit(tmp_path: Path) -> None:
    data = run(copy(tmp_path, "nodump"))
    assert data["meta"]["dumps"] == 0
    t = tcs(data)
    assert "Result Info" in t["TC3-http"]["systemout"]
    (step,) = t["TC2-exec-fails"]["steps"]
    assert (step["n"], step["status"], step["hasDump"]) == (2, "fail", False)


def test_newest_report_wins_for_the_same_suite(tmp_path: Path) -> None:
    out = copy(tmp_path, "xml")
    older = out / "test_results_tests_suite_a.xml"
    shutil.copy(out / "test_results_suite_a.xml", older)
    stamp = time.time() - 86400
    os.utime(older, (stamp, stamp))
    data = run(out)
    assert len(data["suites"]) == 1 and data["suites"][0]["source"] == "test_results_suite_a.xml"


def test_truncation(tmp_path: Path) -> None:
    (step,) = tcs(run(copy(tmp_path, "xml"), "--max-body", "20"))["TC3-http"]["steps"]
    assert "truncated by venom-report" in step["http"]["respBody"]


def test_html_cannot_be_broken_by_a_body(tmp_path: Path) -> None:
    out = copy(tmp_path, "xml")
    run(out)
    html = (out / "report" / "index.html").read_text()
    data_block = re.search(r'id="data">(.*?)</script>', html, re.DOTALL).group(1)
    assert "<" not in data_block


def test_empty_dir_is_an_error(tmp_path: Path) -> None:
    assert vr.main([str(tmp_path)]) == 2


def test_json_format_needs_no_dump(tmp_path: Path) -> None:
    out = copy(tmp_path, "json")
    for p in out.glob("*.dump.json"):
        p.unlink()
    data = run(out)
    (step,) = tcs(data)["TC3-http"]["steps"]
    http = step["http"]
    assert (http["method"], http["url"], http["status"]) == (
        "POST",
        "http://127.0.0.1:18765/x?a=1",
        501,
    )
    assert http["reqHeaders"]["Authorization"] == "***"
    assert "SECRET123" not in (out / "report" / "index.html").read_text()
