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

Exit codes:

    0  clean, or skipped because the repo has no rules
    1  the body violates the repo's rules
    2  the check could not run (vale missing, sync failed, body unreadable)

A caller that gates on this must treat 2 as a failure. A linter that could not
start has not passed.
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
import urllib.request

# The name the body is copied to. The extension is the whole point; see above.
_BODY_FILENAME = "pr-body.md"

# `Packages =` in a vale config declares rules fetched by `vale sync`, not
# committed. When it is present and sync has never run, every one of those rules
# is missing and vale still exits 0 — the same silent-clean shape as the
# extension trap, so it gets the same treatment: sync, or refuse to report.
_PACKAGES = re.compile(r"^\s*Packages\s*=", re.MULTILINE)

# `::error` and friends are workflow commands. A PR body is author-controlled,
# so its text is defanged before it can be echoed into a runner's log.
_WORKFLOW_COMMAND = re.compile(r"^(\s*)::")


def _defang(line: str) -> str:
    return _WORKFLOW_COMMAND.sub(r"\1:​:", line)


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


def lint(body: str, config: str, rules_dir: str, sync: bool = True) -> tuple[int, str]:
    """Lint `body` as markdown. Returns a status and a human-readable report.

    Status 0 clean, 1 alerts, 2 vale could not run. The alert count comes from
    vale's JSON, never from its exit code — see the module docstring on warnings.
    """
    if sync and _PACKAGES.search(_read_text(config)):
        try:
            _sync(config, rules_dir)
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            return 2, f"vale sync failed, so the pinned rules are missing:\n{detail}"

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
        return 2, f"vale did not return a report:\n{detail}"

    alerts = [alert for alerts in report.values() for alert in alerts]
    if not alerts:
        # An empty report from a run that also failed is "could not run", not
        # "clean". Only trust the emptiness when vale was otherwise happy.
        if result.returncode > 1:
            return 2, (result.stderr or result.stdout).strip()
        return 0, ""
    return 1, "\n".join(_render(alert) for alert in alerts)


def _render(alert: dict) -> str:
    """One alert, in the shape an editor and a human both read."""
    return (
        f"PR body:{alert.get('Line', 0)}:{alert.get('Span', [0])[0]}: "
        f"{alert.get('Severity', 'alert')}: {alert.get('Check', '?')}: "
        f"{alert.get('Message', '')}"
    )


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _fetch_body(repo: str, pr: int, token: str) -> tuple[str, bool]:
    """The PR body and whether a bot wrote it, in one REST read.

    Mirrors the sibling checker: urllib against REST, no gh CLI, and a failed
    read raises rather than returning an empty body that would read as clean.
    """
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls/{pr}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gh-pr-body-action",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return payload.get("body") or "", payload.get("user", {}).get("type") == "Bot"


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
        print("prose: vale is not installed, so no verdict is safe.", file=sys.stderr)
        return 2

    if args.body_file:
        try:
            body = sys.stdin.read() if args.body_file == "-" else _read_text(args.body_file)
        except OSError as exc:
            print(f"prose: could not read the body: {exc}", file=sys.stderr)
            return 2
    elif args.repo and args.pr:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print("prose: GITHUB_TOKEN is required to read a PR body.", file=sys.stderr)
            return 2
        try:
            body, is_bot = _fetch_body(args.repo, args.pr, token)
        except Exception as exc:  # noqa: BLE001 - any read failure is "cannot run"
            print(f"prose: could not read PR #{args.pr}: {exc}", file=sys.stderr)
            return 2
        if is_bot:
            print("prose: skipped, the body was written by a bot.")
            return 0
    else:
        parser.error("give --body-file, or both --repo and --pr")

    if not body.strip():
        print("prose: the body is empty, so there is nothing to lint.")
        return 0

    status, output = lint(body, config, args.rules_dir, sync=not args.no_sync)
    if status > 1:
        print(f"prose: vale could not run, so no verdict is safe.\n{output}", file=sys.stderr)
        return 2
    if status == 0:
        print("prose: clean against this repository's rules.")
        return 0

    print("prose: the description does not match this repository's rules.\n")
    print("\n".join(_defang(line) for line in output.splitlines()))
    print(
        "\nThe rules are this repository's own, in .vale.ini. Reproduce locally:\n"
        "    vale --no-global --config .vale.ini <a copy of the body named *.md>"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
