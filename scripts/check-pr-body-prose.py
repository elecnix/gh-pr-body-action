#!/usr/bin/env python3
"""Lint a PR description against the calling repo's own prose rules.

This checker ships **no rules**. It reads `.vale.ini` out of the repository it
is pointed at and runs vale with it. A repo with no `.vale.ini` is skipped, out
loud, with the reason on stdout — never passed silently. That is deliberate: a
prose standard belongs to the project that wrote it, and a linter that invented
one would be enforcing a stranger's taste on every caller.

Because the rules come from the checkout, the calling workflow must run
`actions/checkout` before this step. Without it the workspace has no `.vale.ini`
and the check reports "skipped, no rules found" — which is the same message a
repo that genuinely has no rules gets, and is the honest answer in both cases.

The extension trap
------------------
Vale picks its parser from the file **extension**. A PR body handed over as a
bare path, as a `.txt`, or on stdin lints as *plain text*: every markdown-scoped
rule is skipped, nothing says so, and the run reports clean. A clean report that
could not have found anything is worse than no report at all.

So the body is always copied to a temporary `pr-body.md` before vale sees it,
whatever the source was. `test_check_pr_body_prose.py` proves it: it lints a
body whose only violation lives in a markdown heading, with a rule scoped to
headings, and asserts the violation is found.

Three more vale behaviours this code depends on:

  * **Vale exits 0 on a warning.** Only an `error`-level alert makes it exit
    non-zero, so a rule pack written at `warning` — which is most of them —
    reports every finding and still returns success. Reading the exit code
    would call a violating body clean. So the verdict is read from vale's JSON
    output, and its exit status is used only to tell "found alerts" from "could
    not run at all".
  * `--glob` is a directory-walk filter. Passed alongside an explicit file path
    it prints `{}` and exits 0 — a silent clean verdict on a file it never
    looked at. This checker never passes `--glob`.
  * `--no-global` is mandatory. Without it a developer's personal `~/.vale.ini`
    changes the verdict, and a check that reads a different rule set per machine
    is not a check.

Usage:

    python3 scripts/check-pr-body-prose.py --body-file body.md --rules-dir .
    cat body.md | python3 scripts/check-pr-body-prose.py --body-file -
    python3 scripts/check-pr-body-prose.py --repo OWNER/REPO --pr 1234

The body itself is never read here. `scripts/pr_body_source.py` owns the
file, stdin and REST sources, the token precedence (`GITHUB_TOKEN`, then
`GH_TOKEN`), and the rule that a failed read raises instead of returning an
empty body. This file's own `GITHUB_TOKEN`-only lookup, which silently made
this the one checker a caller exporting `GH_TOKEN` could not run, is gone.

Exit codes:

    0  clean, or skipped because the repo has no rules
    1  the body violates the repo's rules
    2  the check could not run (vale missing, sync failed, body unreadable)

A caller that gates on this must treat 2 as a failure. A linter that could not
start has not passed.

Each alert is reported as a Finding — the same shape the other two checkers
emit, defined in scripts/finding.py — whose pattern is the vale check name.
That name belongs to the calling repository, which is the whole point of this
checker, so no fixed identifier for it is listed in this repo's README.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# The acquisition seam. This checker does not read the PR body; it asks
# scripts/pr_body_source.py for one, the same module the format checker asks.
# Before that it read `GITHUB_TOKEN` and nothing else, so a caller exporting
# only `GH_TOKEN` got a format verdict and a prose check that refused to start.
# Python puts a script's own directory on `sys.path`, so the import resolves
# under every invocation this action uses (`python3 scripts/…`, an absolute path
# from action.yml, and the unit tests).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import pr_body_source  # noqa: E402  (needs the path above)

# scripts/finding.py sits beside this script. action.yml runs this file by
# absolute path from an arbitrary working directory, and the unit tests load it
# by file location, so the import path is derived from __file__ rather than
# assumed.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# pylint: disable=wrong-import-position
from finding import Finding, render_report

# The name the body is copied to. The extension is the whole point; see above.
_BODY_FILENAME = "pr-body.md"

# `Packages =` in a vale config declares rules fetched by `vale sync`, not
# committed. When it is present and sync has never run, every one of those rules
# is missing and vale still exits 0 — the same silent-clean shape as the
# extension trap, so it gets the same treatment: sync, or refuse to report.
_PACKAGES = re.compile(r"^\s*Packages\s*=", re.MULTILINE)

# One sentence, two callers. `lint()` owns the guard — it is the seam that
# promises status 2 for "vale could not run" — and `main()` repeats it only so a
# missing vale costs no body read (and, with --repo/--pr, no network round trip).
# A linter that could not start has not passed, and both paths say the same.
_VALE_MISSING = "vale is not installed, so no verdict is safe."


def find_config(rules_dir: str, explicit: str | None) -> str | None:
    """The vale config to use, or None when the repo carries no rules."""
    candidate = explicit or os.path.join(rules_dir, ".vale.ini")
    return candidate if os.path.isfile(candidate) else None


def _sync(config: str, rules_dir: str) -> None:
    """Fetch the packages the config pins. Raises when they cannot be had."""
    subprocess.run(
        ["vale", "--no-global", f"--config={config}", "sync"],
        cwd=rules_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def lint(
    body: str, config: str, rules_dir: str, sync: bool = True
) -> tuple[int, list[Finding], str]:
    """Lint `body` as markdown. Returns a status, the findings, and a detail.

    Status 0 clean, 1 alerts, 2 vale could not run. The alert count comes from
    vale's JSON, never from its exit code — see the module docstring on warnings.

    Status 2 is this function's own promise, so the guard that keeps it belongs
    here and not in the caller: with vale off PATH this returns 2 rather than
    letting FileNotFoundError escape.

    The findings list is empty on 0 and 1; the detail string carries the reason
    on 2 and is empty otherwise. The detail is deliberately not a Finding: a
    linter that could not start has found nothing, and reporting "no findings"
    for that is the lie this checker exists to avoid.
    """
    if shutil.which("vale") is None:
        return 2, [], _VALE_MISSING

    if sync and _PACKAGES.search(_read_text(config)):
        try:
            _sync(config, rules_dir)
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            return 2, [], f"vale sync failed, so the pinned rules are missing:\n{detail}"

    with tempfile.TemporaryDirectory() as tmp:
        # The copy is what closes the extension trap. Never lint the caller's
        # path directly, whatever it is named.
        target = os.path.join(tmp, _BODY_FILENAME)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(body)
        # No --glob, ever: alongside a file path it reports a clean file it
        # never opened.
        result = subprocess.run(
            ["vale", "--no-global", f"--config={config}", "--output=JSON", target],
            cwd=rules_dir,
            capture_output=True,
            text=True,
        )

    try:
        report = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        detail = (result.stderr or result.stdout).strip()
        return 2, [], f"vale did not return a report:\n{detail}"

    alerts = [alert for alerts in report.values() for alert in alerts]
    if not alerts:
        # An empty report from a run that also failed is "could not run", not
        # "clean". Only trust the emptiness when vale was otherwise happy.
        if result.returncode > 1:
            return 2, [], (result.stderr or result.stdout).strip()
        return 0, [], ""
    return 1, [_finding(alert) for alert in alerts], ""


def _finding(alert: dict) -> Finding:
    """One vale alert as a Finding: the check name is the pattern.

    The pattern belongs to the calling repository's own rule pack, which is why
    this checker's findings cannot be named in this repo's README the way the
    four format patterns are. Everything else is the shared shape: the line vale
    reported, and the severity and message in the renderer's own words.

    The column span stays in the message rather than the head, because the head
    is the same for every checker and the column is the one thing here an editor
    needs. vale reports it as a pair — `[first, last]` — and most alerts are one
    character wide, so both spellings are kept.
    """
    span = alert.get("Span") or [0]
    first, last = int(span[0]), int(span[-1])
    columns = f"column {first}" if first == last else f"columns {first}–{last}"
    line = int(alert.get("Line", 0) or 0)
    return Finding(
        pattern=alert.get("Check", "?"),
        start=line,
        end=line,
        message=(
            f"{columns}: {alert.get('Severity', 'alert')}: "
            f"{alert.get('Message', '')}"
        ),
    )


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint a PR description against the calling repo's own prose rules.",
    )
    parser.add_argument("--repo", help="OWNER/REPO of the pull request")
    parser.add_argument("--pr", type=int, help="pull-request number")
    parser.add_argument(
        "--body-file",
        help="read the body from a file, or '-' for stdin (no network)",
    )
    parser.add_argument(
        "--rules-dir",
        default=".",
        help="checkout holding the rules; default the working directory",
    )
    parser.add_argument("--config", help="path to .vale.ini; default <rules-dir>/.vale.ini")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="skip 'vale sync' even when the config pins packages",
    )
    args = parser.parse_args(argv)

    config = find_config(args.rules_dir, args.config)
    if config is None:
        looked_at = args.config or os.path.join(args.rules_dir, ".vale.ini")
        print(
            "prose: skipped. No prose rules found at "
            f"{looked_at}.\n"
            "       This check carries no rules of its own; it reads the ones the\n"
            "       repository ships. Add a .vale.ini (and run actions/checkout\n"
            "       before this step) to turn it on."
        )
        return 0

    if shutil.which("vale") is None:
        # Duplicated from lint() on purpose: cheap, and it keeps the "cannot
        # run" verdict ahead of reading the body at all.
        print(f"prose: {_VALE_MISSING}", file=sys.stderr)
        return 2

    try:
        if args.body_file:
            body = pr_body_source.read_body_file(args.body_file)
        elif args.repo and args.pr:
            acquired = pr_body_source.fetch_pull_request(args.repo, args.pr)
            if acquired.is_bot:
                print("prose: skipped, the body was written by a bot.")
                return 0
            body = acquired.text
        else:
            parser.error("give --body-file, or both --repo and --pr")
    except pr_body_source.BodyReadError as exc:
        # The seam raises one type for every failed read — an unreadable file, a
        # dead network, a 404, a 200 that is not JSON, no token at all. None of
        # them may be reported as a clean body, so none of them reach the
        # linter.
        print(f"prose: {exc}", file=sys.stderr)
        return 2

    if not body.strip():
        print("prose: the body is empty, so there is nothing to lint.")
        return 0

    status, findings, detail = lint(body, config, args.rules_dir, sync=not args.no_sync)
    if status > 1:
        print(f"prose: vale could not run, so no verdict is safe.\n{detail}", file=sys.stderr)
        return 2
    if status == 0:
        print("prose: clean against this repository's rules.")
        return 0

    print("prose: the description does not match this repository's rules.\n")
    print(render_report(findings, f"{len(findings)} prose finding(s):"))

    print(
        "\nThe rules are this repository's own, in .vale.ini. Reproduce locally:\n"
        "    vale --no-global --config .vale.ini <a copy of the body named *.md>"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
