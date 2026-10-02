#!/usr/bin/env python3
"""Run every PR-body check the action promises, in one place and one order.

This is the whole ladder that used to be three bash steps inside `action.yml`:
which surface to check, which checkers to run, how their exit codes combine,
and what a prose finding is worth. It lives here because every one of those
decisions is a rule, and a rule that lives in a shell script has no test file,
no CI job, and no way to be wrong loudly. The shellcheck job in `ci.yml` globs
`scripts/*.sh`; a `run:` block inlined in `action.yml` is not a file and was
never linted, let alone tested.

Three promises this file keeps, each of which was previously unenforceable:

  1. **Both format checkers run even when one is red.** An author with a
     hard-wrapped paragraph AND a diagram the mermaid parser rejects should see
     both findings in one push, not one per push. Neither checker is allowed to
     cancel the other, so there is no `set -e` between them.

  2. **Severity survives the combination.** The checkers agree on three codes:
     0 clean, 1 violation, 2 could not read or could not run. The old wiring
     combined them with bash arithmetic — `rc=$(( rc1 || rc2 ))` — and `||` in
     arithmetic yields 0 or 1 and nothing else, so every read failure collapsed
     into 1 and reached the caller indistinguishable from an ordinary
     formatting violation. That is precisely the lie the format checker's own
     docstring exists to prevent ("a failed read is never reported as a clean
     body"), and it was the composed path that undid it. Combining with the
     most severe code keeps the ladder honest: a 2 still reports as a 2.

  3. **Prose never short-circuits formatting, and a prose finding is not a
     failure unless the repo asked for one.** Ordering falls out of running
     both in this process. The escalation is the interesting half: a finding
     (1) fails only when `prose-fail` is true, while a linter that could not
     run at all (2) fails regardless — a check that did not start has not
     passed.

Mode selection lives here too, including the mutual exclusion the `comment`
input documents and never enforced. Wiring both `pr` and `comment` used to
check the comment for formatting and lint the PR body for prose in one run,
with no warning: two different surfaces, two verdicts, one green-looking job.
That is now a usage error (exit 2), which is the code the `violations` output
already documented for "usage failure".

Usage:

    python3 scripts/check-pr-body-orchestrator.py --repo OWNER/REPO --pr 1234
    python3 scripts/check-pr-body-orchestrator.py --repo OWNER/REPO --comment 99
    python3 scripts/test_check_pr_body_orchestrator.py

Exit codes:

    0  everything clean (a prose finding may still have been reported)
    1  at least one violation, or a prose finding with --prose-fail
    2  the action was wired in a way that cannot be served, or a checker could
       not read its input

Outputs written to `--github-output` (default: `$GITHUB_OUTPUT`):

    rc         the combined format/mermaid code — the action's `violations`
    prose_rc   the prose checker's own code — the action's `prose`

Keeping them separate is the point. `prose_rc` is the raw verdict; `rc` is the
format ladder's verdict, exactly what `violations` has always meant. Neither is
the process's exit status, which is the two *escalated* together because that is
the only number GitHub turns into a step conclusion: a blocking prose finding
on an otherwise clean body fails the step without pretending the body was a
formatting violation, and a reported one annotates without reddening.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# The checkers live beside this file, which is why `action.yml` passes no path:
# the orchestrator is the only thing that has to know where they are, and it is
# the thing that sits next to them.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

_FORMAT_CHECKER = "check-pr-body-format.py"
_MERMAID_CHECKER = "check-pr-body-mermaid.mjs"
_PROSE_CHECKER = "check-pr-body-prose.py"

CLEAN = 0
VIOLATION = 1
UNRUNNABLE = 2


class UsageError(Exception):
    """The action was wired in a way that cannot be served.

    Raised before anything is fetched or run, so a miswired call costs no API
    call and no linter run.
    """


def combine(*codes: int) -> int:
    """Combine checker exit codes, keeping the most severe one.

    The old bash used `rc=$(( rc1 || rc2 ))`. Bash arithmetic's `||` is a
    boolean operator that yields 0 or 1, so it silently answered 1 for every
    combination that included a 2 — a total read failure was reported to the
    caller as an ordinary formatting violation. Severity is an ordering, not a
    boolean, and `max` is that ordering.

    `max` is the whole rule: 2 outranks 1, 1 outranks 0. A run that found a
    formatting violation *and* could not read the body reports 2, because the
    unread body is the larger unknown.
    """
    return max(codes, default=CLEAN)


def escalate_prose(prose_rc: int, prose_fail: bool) -> tuple[int, str | None]:
    """What the prose checker is worth: a step status and an annotation.

    `prose_rc` is the checker's own code. The returned code is what this step
    contributes to the run's exit status, and it is deliberately not always the
    same number:

      * 0 — clean, or skipped for a stated reason. Contributes nothing.
      * 1 — a finding. Contributes a failure only when the repo set
        `prose-fail`; otherwise it annotates and the run stays green.
      * 2 — the linter never ran (vale missing, `vale sync` failed, the body
        unreadable). Contributes a failure whatever `prose-fail` says, because
        a check that did not start has not passed.

    The annotation level is `error` for both failing cases and `warning` for a
    reported finding, so a repo that asked for blocking gets an annotation and
    one that did not gets the same report without the red.
    """
    if prose_rc >= UNRUNNABLE:
        return 1, "error"
    if prose_rc == VIOLATION:
        return (1, "error") if prose_fail else (0, "warning")
    return CLEAN, None


def select_mode(pr: str | None, comment: str | None) -> str:
    """Which surface to check: `pr`, `comment`, or `none`.

    Raises `UsageError` when both are given. The `comment` input documents the
    two as mutually exclusive, and the format checker refuses the combination at
    its own argparse level — but the composite action branched on one input and
    gated prose on the other, so it never got there: a caller wiring both got
    the comment format-checked and the PR body prose-linted in the same run,
    neither verdict mentioning the other.
    """
    pr = (pr or "").strip()
    comment = (comment or "").strip()
    if pr and comment:
        raise UsageError(
            "`comment` and `pr` are mutually exclusive, and both are set "
            f"(pr={pr}, comment={comment}). Comment mode checks the comment "
            "and skips prose, because prose lints a pull-request description "
            "and a comment is not one. Wire one or the other."
        )
    if comment:
        return "comment"
    if pr:
        return "pr"
    return "none"


def _numeric(value: str, flag: str) -> str:
    """Validate an id before it reaches a checker's argparse.

    Action inputs are strings and may arrive empty or as anything at all; a
    `type=int` on this script's own arguments would turn an empty
    `--pr ""` from a passed event default into a usage traceback.
    """
    try:
        number = int(value)
    except ValueError:
        raise UsageError(f"`{flag}` must be a number, got {value!r}.") from None
    if number <= 0:
        raise UsageError(f"`{flag}` must be a positive id, got {value!r}.")
    return str(number)


def _bool_flag(value: str | bool, flag: str) -> bool:
    """Read an action's `'true'`/`'false'` string as a boolean."""
    if isinstance(value, bool):
        return value
    lowered = (value or "").strip().lower()
    if lowered in ("true", "1", "yes", "on", ""):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    raise UsageError(f"`{flag}` must be 'true' or 'false', got {value!r}.")


def _run(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Run a checker and hand back its exit code, whatever it is.

    No `check=True`: this function exists so that a non-zero status is data
    rather than an exception, which is the only way both checkers can run when
    the first one is already red.
    """
    return subprocess.run(argv, env=env).returncode


def run_format_checker(repo: str, pr: str | None = None, comment: str | None = None) -> int:
    """The Python line classifier."""
    argv = [sys.executable, os.path.join(_SCRIPTS_DIR, _FORMAT_CHECKER), "--repo", repo]
    if comment:
        argv += ["--comment", comment]
    else:
        argv += ["--pr", pr or ""]
    return _run(argv)


def run_mermaid_checker(repo: str, pr: str) -> int:
    """The Node checker, against the real mermaid parser."""
    return _run(
        ["node", os.path.join(_SCRIPTS_DIR, _MERMAID_CHECKER), "--repo", repo, "--pr", pr]
    )


def run_prose_checker(repo: str, pr: str, rules_dir: str) -> int:
    """The vale wrapper. Its own docstring covers the traps it closes."""
    return _run(
        [
            sys.executable,
            os.path.join(_SCRIPTS_DIR, _PROSE_CHECKER),
            "--repo",
            repo,
            "--pr",
            pr,
            "--rules-dir",
            rules_dir,
        ]
    )


def check_formatting(repo: str, pr: str | None, comment: str | None) -> int:
    """Run the format ladder for the selected surface and combine the results.

    In PR mode both checkers run, in order, and the second is not cancelled by
    the first being red. In comment mode the mermaid checker is skipped: it has
    no comment mode, and a diagram in a comment is not rendered by GitHub, so
    there is nothing for the parser to disagree with.
    """
    if comment:
        return run_format_checker(repo, comment=comment)
    format_rc = run_format_checker(repo, pr=pr)
    mermaid_rc = run_mermaid_checker(repo, pr or "")
    return combine(format_rc, mermaid_rc)


def _write_outputs(path: str | None, violations: int, prose_rc: int) -> None:
    """Publish the two codes the action's outputs promise.

    They are deliberately not the same number, and neither is the step's own
    exit status:

      * `violations` is the format ladder's verdict — the format and mermaid
        checkers combined, with severity preserved. This is what it has always
        meant, so a caller reading it for a violation count is unaffected by
        whether prose ran.
      * `prose_rc` is the prose checker's own verdict, which the prose
        escalation then decides the weight of.
      * the process's exit status is the two *escalated* together, because that
        is the only number GitHub turns into a step conclusion.

    Guarded, not asserted: the script is also runnable by hand, where there is
    no `GITHUB_OUTPUT` and nothing to publish to.
    """
    target = path or os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"rc={violations}\n")
        handle.write(f"prose_rc={prose_rc}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the PR-body format, mermaid, and prose checks in order.",
    )
    parser.add_argument("--repo", default=os.environ.get("PR_REPO", ""),
                        help="OWNER/REPO of the pull request or comment")
    parser.add_argument("--pr", default=os.environ.get("PR_NUMBER", ""),
                        help="pull-request number; empty for none")
    parser.add_argument("--comment", default=os.environ.get("COMMENT_ID", ""),
                        help="pull-request comment id; empty for none")
    parser.add_argument("--prose", default=os.environ.get("PROSE", "true"),
                        help="'true' to run the prose check")
    parser.add_argument("--prose-fail", default=os.environ.get("PROSE_FAIL", "false"),
                        help="'true' to make a prose finding fail the run")
    parser.add_argument("--rules-dir", default=os.environ.get("RULES_DIR", "."),
                        help="checkout holding the .vale.ini")
    parser.add_argument("--github-output", default=None,
                        help="file to append rc= and prose_rc= to "
                             "(default: $GITHUB_OUTPUT)")
    args = parser.parse_args(argv)

    try:
        mode = select_mode(args.pr, args.comment)
        if mode == "none":
            # Not a pull_request and not an issue_comment event. Nothing to
            # read and nothing to report: green, and the reason out loud.
            print(
                "orchestrator: no pull-request number and no comment id "
                "(not a pull_request or issue_comment event); skipping."
            )
            _write_outputs(args.github_output, CLEAN, CLEAN)
            return CLEAN
        if mode == "comment":
            comment = _numeric((args.comment or "").strip(), "comment")
        else:
            comment = None
        pr = _numeric((args.pr or "").strip(), "pr") if mode == "pr" else None
        if not args.repo:
            raise UsageError("`repo` is required.")
        prose_enabled = _bool_flag(args.prose, "prose")
        prose_fail = _bool_flag(args.prose_fail, "prose-fail")
    except UsageError as exc:
        print(f"::error::{exc}")
        _write_outputs(args.github_output, UNRUNNABLE, UNRUNNABLE)
        return UNRUNNABLE

    format_rc = check_formatting(args.repo, pr, comment)

    prose_rc = CLEAN
    if mode == "comment":
        # Said out loud because `prose` defaults to true: a caller who wired
        # comment mode and expected the prose step to run was never told it
        # would not.
        print(
            f"orchestrator: comment mode, so the prose check did not run — it "
            f"lints a pull-request description, and comment {comment} is not "
            "one. Set `prose` and `comment` in separate steps to lint both."
        )
    elif not prose_enabled:
        print("orchestrator: the prose check is turned off (`prose: 'false'`).")
    else:
        # Formatting first, and neither way round: a wrapped paragraph and an
        # off-house sentence are one author's single mistake and should not cost
        # two pushes to find.
        prose_rc = run_prose_checker(args.repo, pr or "", args.rules_dir)

    prose_step_rc, annotation = escalate_prose(prose_rc, prose_fail)
    if annotation == "error":
        print("::error::The prose check could not run, so no verdict is safe.")
    elif annotation == "warning":
        print(
            "::warning::The description does not match this repository's prose "
            "rules; see the step log."
        )

    rc = combine(format_rc, prose_step_rc)
    # The output keeps the format ladder's own verdict; the exit status is the
    # whole ladder. A blocking prose finding on an otherwise clean body fails
    # the step without pretending the body was a formatting violation.
    _write_outputs(args.github_output, format_rc, prose_rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
