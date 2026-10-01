"""What venom writes, read into one model: JUnit xml or json reports, and the
per-step *.dump.json files of -vvv runs. Secrets are masked by Redactor."""

from __future__ import annotations

import base64
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DUMP_RE = re.compile(r"^(?P<prefix>.+)\.testcase\.(?P<tc>\d+)\.step\.(?P<n>\d+)\.(?P<r>\d+)\.dump\.json$")
STEP_REF_RE = re.compile(r"step #(\d+)-(\d+)")
ASSERTION_RE = re.compile(r'Assertion "(.*?)" failed')
# A dump is kept only if written inside its suite's run window, plus this slack:
# output/ is never cleaned by venom, so dumps of older runs sit next to fresh ones.
WINDOW_SLACK = 60.0
MASK = "***"


# --------------------------------------------------------------------------- redaction


class Redactor:
    """Masks values under sensitive keys, then every occurrence of those values."""

    def __init__(self, pattern: str) -> None:
        self.key_re = re.compile(pattern, re.IGNORECASE)
        self.values: set[str] = set()

    def learn(self, mapping: Any) -> None:
        """Collect secret values (from dump variables, suite secrets) to scrub everywhere."""
        if isinstance(mapping, dict):
            for k, v in mapping.items():
                if isinstance(v, (dict, list)):
                    self.learn(v)
                elif self.key_re.search(str(k)) and isinstance(v, str) and len(v) >= 6:
                    self.values.add(v)
                    # "Bearer abc" -> also scrub "abc" alone
                    self.values.update(p for p in v.split() if len(p) >= 12)
        elif isinstance(mapping, list):
            for v in mapping:
                self.learn(v)

    def text(self, s: str) -> str:
        for v in sorted(self.values, key=len, reverse=True):
            s = s.replace(v, MASK)
        return re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 " + MASK, s)

    def obj(self, o: Any) -> Any:
        if isinstance(o, dict):
            return {
                k: (MASK if self.key_re.search(str(k)) and not isinstance(v, (dict, list)) else self.obj(v))
                for k, v in o.items()
            }
        if isinstance(o, list):
            return [self.obj(v) for v in o]
        if isinstance(o, str):
            return self.text(o)
        return o


# --------------------------------------------------------------------------- model


@dataclass
class Step:
    n: int
    r: int
    type: str = ""
    name: str = ""
    status: str = "pass"  # pass | fail | unknown
    errors: list[str] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)  # {text, ok}
    http: dict[str, Any] | None = None
    systemout: str = ""
    systemerr: str = ""
    raw: Any = None
    has_dump: bool = False


@dataclass
class Testcase:
    name: str
    index: int
    status: str  # pass | fail | skip
    time: float = 0.0
    messages: list[str] = field(default_factory=list)
    skip_reason: str = ""
    systemout: str = ""
    steps: list[Step] = field(default_factory=list)


@dataclass
class Suite:
    name: str
    file: str
    stem: str
    source: str
    time: float
    end: float  # epoch of the end of the run (mtime of the report file, or JSON end)
    start: float | None = None  # epoch of the start of the RUN it belongs to, see run_windows()
    testcases: list[Testcase] = field(default_factory=list)


def _stem(filename: str) -> str:
    return re.sub(r"\.ya?ml$", "", Path(filename).name)


def _epoch(iso: str | None, fallback: float) -> float:
    if not iso:
        return fallback
    try:  # venom writes nanoseconds, fromisoformat takes microseconds
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", iso)).timestamp()
    except ValueError:
        return fallback


# --------------------------------------------------------------------------- readers


def read_xml(path: Path) -> list[Suite]:
    suites = []
    root = ET.parse(path).getroot()
    nodes = [root] if root.tag == "testsuite" else root.findall("testsuite")
    for ts in nodes:
        file = ts.get("package") or ""
        cases = ts.findall("testcase")
        file = file or (cases[0].get("classname", "") if cases else "")
        suite = Suite(
            name=ts.get("name", file),
            file=file,
            stem=_stem(file),
            source=path.name,
            time=float(ts.get("time") or 0),
            end=path.stat().st_mtime,
        )
        for i, tc in enumerate(cases, 1):
            msgs = [(e.text or "").strip() for e in tc if e.tag in ("failure", "error")]
            skipped = tc.find("skipped")
            status = "fail" if msgs else "skip" if skipped is not None else "pass"
            suite.testcases.append(
                Testcase(
                    name=tc.get("name", ""),
                    index=i,
                    status=status,
                    time=float(tc.get("time") or 0),
                    messages=msgs,
                    skip_reason=(skipped.text or "").strip() if skipped is not None else "",
                    systemout=(tc.findtext("system-out") or "").strip(),
                )
            )
        suites.append(suite)
    return suites


def _decode_yaml(b64: str | None) -> str:
    if not b64:
        return ""
    try:
        return base64.b64decode(b64).decode("utf-8", "replace")
    except ValueError:
        return ""


def _unflatten_result(computed: dict[str, Any] | None) -> dict[str, Any]:
    """computedVars flattens the result: `result.request.header.Authorization.Authorization0`
    is a leaf, `result.request` its Go-printed parent, `__Type__`/`__Len__` metadata.
    Rebuild the tree from the leaves; bodyjson is dropped (body carries the same)."""
    flat = {
        k[7:]: v for k, v in (computed or {}).items() if k.startswith("result.") and not k.startswith("result.bodyjson")
    }
    types = {k[: -len(".__Type__")]: v for k, v in flat.items() if k.endswith(".__Type__")}
    parents = {k.rsplit(".", i)[0] for k in flat for i in range(1, k.count(".") + 1)}
    tree: dict[str, Any] = {}
    for k, v in flat.items():
        if k in parents or k.rsplit(".", 1)[-1].startswith("__"):
            continue
        node = tree
        *path, leaf = k.split(".")
        for part in path:
            node = node.setdefault(part, {})
        node[leaf] = v

    def arrays(node: Any, path: str) -> Any:
        if not isinstance(node, dict):
            return node
        node = {k: arrays(v, f"{path}.{k}" if path else k) for k, v in node.items()}
        return list(node.values()) if types.get(path) == "Array" else node

    res = arrays(tree, "")
    if str(res.get("statuscode", "")).isdigit():
        res["statuscode"] = int(res["statuscode"])
    return res


def read_json(path: Path, redactor: Redactor) -> list[Suite]:
    data = json.loads(path.read_text())
    suites = []
    for ts in data.get("test_suites") or []:
        for s in ts.get("secrets") or []:
            if isinstance(s, str) and len(s) >= 6:
                redactor.values.add(s)
        file = ts.get("filename") or ts.get("filepath") or ""
        mtime = path.stat().st_mtime
        suite = Suite(
            name=ts.get("name", file),
            file=file,
            stem=ts.get("shortname") or _stem(file),
            source=path.name,
            time=float(ts.get("duration") or 0),
            end=_epoch(ts.get("end"), mtime),
            start=_epoch(ts.get("start"), mtime - float(ts.get("duration") or 0)),
        )
        for i, tc in enumerate(ts.get("testcases") or [], 1):
            redactor.learn(tc.get("vars"))
            st = (tc.get("status") or "").upper()
            status = {"FAIL": "fail", "SKIP": "skip"}.get(st, "pass")
            steps, msgs = [], []
            for res in tc.get("results") or []:
                errs = [e.get("value", "") for e in res.get("errors") or []]
                msgs += errs
                applied = (res.get("assertionsApplied") or {}).get("assertions") or []
                result = _unflatten_result(res.get("computedVars"))
                yaml_def = _decode_yaml(res.get("interpolated"))
                typ = re.search(r"^type:\s*(\S+)", yaml_def, re.MULTILINE)
                step = Step(
                    n=int(res.get("number") or 0),
                    r=int(res.get("rangedIndex") or 0),
                    type=typ[1] if typ else res.get("name") or "",
                    name=res.get("name") or "",
                    status="fail" if (res.get("status") or "").upper() == "FAIL" else "pass",
                    errors=errs,
                    assertions=[{"text": a.get("assertion", ""), "ok": bool(a.get("isOK"))} for a in applied],
                    systemout=res.get("systemout") or "",
                    systemerr=res.get("systemerr") or "",
                    raw={
                        "step": yaml_def,
                        "result": {k: v for k, v in result.items() if k not in ("body", "bodyjson")},
                    },
                )
                if "statuscode" in result or "request" in result:
                    step.http = _http(result)
                steps.append(step)
            suite.testcases.append(
                Testcase(
                    name=tc.get("name", ""),
                    index=i,
                    status=status,
                    time=float(tc.get("duration") or 0),
                    messages=msgs if status == "fail" else [],
                    skip_reason="\n".join(s.get("value", "") for s in tc.get("skipped") or []),
                    steps=steps,
                )
            )
        suites.append(suite)
    return suites


def _pretty(body: Any) -> str:
    """Indent JSON payloads; anything else is returned as is."""
    body = "" if body is None else body if isinstance(body, str) else json.dumps(body)
    try:  # from the raw body: bodyjson is lossy (venom maps arrays to objects)
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except ValueError:
        return body


def _http(result: dict[str, Any]) -> dict[str, Any]:
    req = result.get("request") or {}
    return {
        "method": req.get("method", ""),
        "url": req.get("url", ""),
        "reqHeaders": {
            k: (v[0] if isinstance(v, list) and len(v) == 1 else v) for k, v in (req.get("header") or {}).items()
        },
        "reqBody": _pretty(req.get("body")),
        "status": result.get("statuscode"),
        "respHeaders": result.get("headers") or {},
        "respBody": _pretty(result.get("body")),
        "time": result.get("timeseconds"),
    }


# --------------------------------------------------------------------------- dumps


def index_dumps(out: Path) -> dict[str, list[tuple[int, int, int, Path, float]]]:
    idx: dict[str, list[tuple[int, int, int, Path, float]]] = {}
    for p in out.glob("*.dump.json"):
        m = DUMP_RE.match(p.name)
        if m:
            idx.setdefault(m["prefix"], []).append((int(m["tc"]), int(m["n"]), int(m["r"]), p, p.stat().st_mtime))
    return idx


def run_windows(suites: list[Suite]) -> None:
    """venom writes EVERY xml report at the end of the whole run, so a suite's dumps
    may predate its xml by the duration of all the suites of that run. Reports
    written within a few seconds of each other are one run."""
    group: list[Suite] = []
    for s in sorted((s for s in suites if s.start is None), key=lambda s: s.end) + [None]:  # type: ignore[list-item]
        if group and (s is None or s.end - group[-1].end > 10):
            start = group[-1].end - sum(g.time for g in group)
            for g in group:
                g.start = start
            group = []
        if s is not None:
            group.append(s)


def attach_dumps(suite: Suite, idx: dict, redactor: Redactor) -> int:
    lo = (suite.start if suite.start is not None else suite.end - suite.time) - WINDOW_SLACK
    hi = suite.end + WINDOW_SLACK
    attached = 0
    for tc in suite.testcases:
        found = [d for d in idx.get(f"{suite.stem}.{tc.name}", []) if lo <= d[4] <= hi]
        if not found:
            continue
        tcs = {d[0] for d in found}
        pick = tc.index if tc.index in tcs else max(found, key=lambda d: d[4])[0]
        by_key = {(s.n, s.r): s for s in tc.steps}
        for _, n, r, p, _ in sorted(d for d in found if d[0] == pick):
            try:
                dump = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            redactor.learn(dump.get("variables"))
            step = by_key.get((n, r)) or Step(n=n, r=r)
            by_key[(n, r)] = step
            sdef, result = dump.get("step") or {}, dump.get("result") or {}
            step.has_dump = True
            step.type = step.type or sdef.get("type", "")
            step.name = sdef.get("name") or step.name
            if not step.assertions:
                step.assertions = [
                    {"text": a if isinstance(a, str) else json.dumps(a), "ok": None}
                    for a in sdef.get("assertions") or []
                ]
            if "statuscode" in result or "request" in result:
                step.http = _http(result)
            step.systemout = step.systemout or str(result.get("systemout") or "")
            step.systemerr = step.systemerr or str(result.get("systemerr") or "")
            step.raw = {
                "step": sdef,
                "result": {k: v for k, v in result.items() if k not in ("body", "bodyjson")},
            }
            attached += 1
        tc.steps = [by_key[k] for k in sorted(by_key)]
    return attached


def resolve_statuses(tc: Testcase) -> None:
    """XML only says which testcase failed: map `step #N-R` back to steps and assertions."""
    by_key = {(s.n, s.r): s for s in tc.steps}
    for msg in tc.messages:
        m = STEP_REF_RE.search(msg)
        if not m:
            continue
        key = (int(m[1]), int(m[2]))
        step = by_key.get(key)
        if step is None:  # errored before producing a result: venom wrote no dump
            step = by_key[key] = Step(n=key[0], r=key[1], status="fail")
        if msg not in step.errors:
            step.errors.append(msg)
        step.status = "fail"
        failed = set(ASSERTION_RE.findall(msg))
        for a in step.assertions:
            if a["ok"] is None and a["text"] in failed:
                a["ok"] = False
    for s in by_key.values():
        # venom quotes EVERY failed assertion of a step, so the unquoted ones passed;
        # unless nothing was quoted (the step errored before asserting): unknown.
        quoted = any(a["ok"] is False for a in s.assertions)
        for a in s.assertions:
            if a["ok"] is None:
                a["ok"] = True if s.status != "fail" or quoted else None
    tc.steps = [by_key[k] for k in sorted(by_key)]
