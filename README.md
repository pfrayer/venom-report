# venom-report

[![PyPI](https://img.shields.io/pypi/v/venom-report)](https://pypi.org/project/venom-report/)
[![Python](https://img.shields.io/pypi/pyversions/venom-report)](https://pypi.org/project/venom-report/)
[![CI](https://github.com/pfrayer/venom-report/actions/workflows/ci.yml/badge.svg)](https://github.com/pfrayer/venom-report/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/venom-report)](LICENSE)

A static HTML report for any [venom](https://github.com/ovh/venom) run, like `coverage html`:
every suite, testcase and step, the HTTP call made (method, URL, headers, body), the
response, the assertions (the failing one in red), and why a testcase was skipped.

**[Live demo →](https://pfrayer.github.io/venom-report/)**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/pfrayer/venom-report/main/docs/screenshot-dark.png">
  <img alt="venom-report: a POST step with its JSON request and response, a failing testcase listed on top" src="https://raw.githubusercontent.com/pfrayer/venom-report/main/docs/screenshot-light.png">
</picture>

```sh
pipx install venom-report          # or: uv tool install venom-report
venom-report path/to/output        # -> path/to/output/report/index.html
venom-report output --open         # and open it
```

No dependency (Python ≥ 3.10). The page is one self-contained file that opens over `file://`.

venom writes its reports only with `--output-dir` (or `output_dir:` in `.venomrc`): point
`venom-report` at that directory.

## What it reads

| venom writes | when | gives |
|---|---|---|
| `test_results_*.xml` | `format: xml` (default) | suites, testcases, pass/fail/skip, failure messages |
| `test_results_*.json` | `format: json` | same, plus every step with its request/response, no dump needed |
| `*.dump.json` | `-vvv` / `verbosity: 3` | each step: the request sent and the full response |

Without dumps (xml, no `-vvv`), the report falls back on the JUnit `system-out` and says so.

venom never cleans `output/`: dumps from older runs sit next to fresh ones. A dump is
used only if it was written during the run that produced the report (from its mtime).
If the same suite was reported twice (e.g. `venom run tests/` then `venom run tests/x.yml`),
the newest report wins.

## Options

| option | default | |
|---|---|---|
| `-o FILE` | `OUTPUT_DIR/report/index.html` | where to write |
| `--title T` | `venom · <parent dir>` | page title |
| `--redact REGEX` | `authorization\|token\|secret\|passw(or)?d\|cookie\|api[-_]?key\|credential` | keys and headers whose values are masked |
| `--max-body N` | `100000` | bodies longer than N chars are truncated |
| `--open` | | open the report in a browser |

## Secrets

Values under a sensitive key (in dumps, headers, venom vars, venom `secrets`) are masked,
then every other occurrence of those values is masked too (URLs, bodies, logs), as is
any `Bearer …` / `Basic …`. The venom variables block is never embedded. Still, check a
report before sharing it outside your team.

## Tests

```sh
uv run pytest
```

The fixtures are real venom v1.3.0 outputs of `tests/fixtures/suite_a.yml`.

## Release

Publish a GitHub release with a `vX.Y.Z` tag: the version comes from the tag, and the
`Release` workflow uploads to PyPI (trusted publishing, no token).

## License

[Apache-2.0](LICENSE)
