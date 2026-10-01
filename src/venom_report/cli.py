"""Turn a venom output directory into a single static HTML report.

    venom-report [OUTPUT_DIR] [-o index.html]

Reads what venom writes, whatever the suite:
  - test_results_*.xml (format: xml) or test_results_*.json (format: json)
  - *.dump.json, one per executed step, when venom ran with -vvv / verbosity: 3

Stdlib only. The report never reflects the test status in its exit code: venom
already does that.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import __version__
from .readers import (
    Redactor,
    Suite,
    attach_dumps,
    index_dumps,
    read_json,
    read_xml,
    resolve_statuses,
    run_windows,
)

DEFAULT_REDACT = r"authorization|token|secret|passw(or)?d|cookie|api[-_]?key|credential"


# --------------------------------------------------------------------------- output


def _truncate(s: Any, limit: int) -> Any:
    if isinstance(s, str) and len(s) > limit:
        return s[:limit] + f"\n\n… truncated by venom-report ({len(s)} bytes, --max-body {limit})"
    return s


def serialize(suites: list[Suite], redactor: Redactor, max_body: int) -> list[dict[str, Any]]:
    out = []
    for s in suites:
        tcs = []
        for tc in s.testcases:
            steps = []
            for st in tc.steps:
                http = None
                if st.http:
                    http = redactor.obj(st.http)
                    http["reqBody"] = _truncate(http["reqBody"], max_body)
                    http["respBody"] = _truncate(http["respBody"], max_body)
                raw = redactor.obj(st.raw)
                raw_s = raw if isinstance(raw, str) else json.dumps(raw, indent=2, ensure_ascii=False, default=str)
                steps.append(
                    {
                        "n": st.n,
                        "r": st.r,
                        "type": st.type,
                        "name": st.name,
                        "status": st.status,
                        "errors": [redactor.text(e) for e in st.errors],
                        "assertions": redactor.obj(st.assertions),
                        "http": http,
                        "hasDump": st.has_dump,
                        "systemout": _truncate(redactor.text(st.systemout.strip()), max_body),
                        "systemerr": _truncate(redactor.text(st.systemerr.strip()), max_body),
                        "raw": _truncate(raw_s, max_body) if st.raw else "",
                    }
                )
            tcs.append(
                {
                    "name": tc.name,
                    "index": tc.index,
                    "status": tc.status,
                    "time": tc.time,
                    "messages": [redactor.text(m) for m in tc.messages],
                    "skipReason": redactor.text(tc.skip_reason),
                    "systemout": _truncate(redactor.text(tc.systemout), max_body),
                    "steps": steps,
                }
            )
        out.append(
            {
                "name": s.name,
                "file": s.file,
                "source": s.source,
                "time": s.time,
                "end": s.end,
                "testcases": tcs,
            }
        )
    return out


def build(
    out_dir: Path,
    redact: str,
    max_body: int,
    title: str | None,
) -> tuple[str, dict]:
    redactor = Redactor(redact)
    suites: dict[str, Suite] = {}
    for p in sorted(out_dir.glob("test_results*.xml")) + sorted(out_dir.glob("test_results*.json")):
        try:
            found = read_xml(p) if p.suffix == ".xml" else read_json(p, redactor)
        except (ET.ParseError, ValueError, OSError) as exc:
            print(f"warning: {p.name} skipped ({exc})", file=sys.stderr)
            continue
        for s in found:  # the same suite run twice (dir vs file spelling): keep the newest
            key = s.file or s.name
            if key not in suites or s.end > suites[key].end:
                suites[key] = s
    idx = index_dumps(out_dir)
    dumps = 0
    run_windows(list(suites.values()))
    for s in suites.values():
        dumps += attach_dumps(s, idx, redactor)
        for tc in s.testcases:
            resolve_statuses(tc)
    ordered = sorted(suites.values(), key=lambda s: s.file or s.name)
    meta = {
        "title": title or f"venom · {out_dir.resolve().parent.name}",
        "outputDir": str(out_dir.resolve()),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dumps": dumps,
    }
    data = {"meta": meta, "suites": serialize(ordered, redactor, max_body)}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    template = (
        files(__package__).joinpath("template.html").read_text().replace("__TITLE__", _html_escape(meta["title"]))
    )
    html = template.replace("/*__DATA__*/", payload)
    return html, data


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="venom-report", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "output_dir",
        nargs="?",
        default="output",
        type=Path,
        help="venom output dir (default: ./output)",
    )
    ap.add_argument(
        "-o",
        "--out",
        type=Path,
        help="report file (default: OUTPUT_DIR/report/index.html)",
    )
    ap.add_argument("--title", help="page title")
    ap.add_argument(
        "--redact",
        default=DEFAULT_REDACT,
        help=f"regex of sensitive keys (default: {DEFAULT_REDACT})",
    )
    ap.add_argument(
        "--max-body",
        type=int,
        default=100_000,
        help="truncate bodies above N chars (default: 100000)",
    )
    ap.add_argument("--open", action="store_true", help="open the report in a browser")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)
    if not args.output_dir.is_dir():
        print(f"error: {args.output_dir} is not a directory", file=sys.stderr)
        return 2
    html, data = build(args.output_dir, args.redact, args.max_body, args.title)
    if not data["suites"]:
        print(f"error: no test_results_*.xml|json in {args.output_dir}", file=sys.stderr)
        return 2
    out = args.out or args.output_dir / "report" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    tcs = [tc for s in data["suites"] for tc in s["testcases"]]
    count = {k: sum(tc["status"] == k for tc in tcs) for k in ("pass", "fail", "skip")}
    print(
        f"{len(data['suites'])} suites · {len(tcs)} testcases · {count['pass']} passed · "
        f"{count['fail']} failed · {count['skip']} skipped · {data['meta']['dumps']} step dumps"
    )
    print(f"report: {out.resolve()}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0
