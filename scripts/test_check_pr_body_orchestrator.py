#!/usr/bin/env python3
"""Unit tests for scripts/check-pr-body-orchestrator.py.

This file exists because the logic it covers had no test surface at all: it
lived in three `run:` blocks inside `action.yml`, and `ci.yml`'s shellcheck job
globs `scripts/*.sh`, so an inlined `run:` block was never linted, let alone
exercised. The three rules below are the ones that were unenforceable from bash,
and each has at least one test that fails against the old wiring.

  * `combine` — the severity of a checker is not a boolean. The old bash used
    `rc=$(( rc1 || rc2 ))`, which answers 1 for *every* combination containing a
    2, so a total read failure reached the caller as an ordinary formatting
    violation. `CombineTests` walks the whole 3x3 table;
    `test_bash_or_collapses_a_read_failure` runs the old expression to prove the
    defect was real rather than remembered.
  * `select_mode` — `comment` and `pr` are documented as mutually exclusive and
    nothing enforced it; a caller wiring both got the comment format-checked and
    the PR body prose-linted in one run, with no warning.
  * `escalate_prose` — a finding fails only when `prose-fail` is set, but a
    linter that could not run fails regardless.

`OrderingTests` covers the shape the ladder promises on top of those: both
format checkers run even when the first is red, the mermaid checker is skipped
in comment mode, and prose runs after formatting rather than instead of it.

Nothing here touches the network: `_run` is replaced with a recorder, so these
are decisions about which command runs with which arguments, in which order,
and what the resulting exit code does.

Run: python3 scripts/test_check_pr_body_orchestrator.py
"""

from __future__ import annotations

import importlib.util
import io
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "check-pr-body-orchestrator.py")

_spec = importlib.util.spec_from_file_location("check_pr_body_orchestrator", _SCRIPT)
orch = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(orch)


class _Recorder:
    """Stand in for `subprocess.run`: records argv, returns scripted codes."""

    def __init__(self, codes=None, default=0):
        # A dict keyed by a substring of the checker path keeps the tests
        # readable: {"mermaid": 1} rather than positional guesswork.
        self.codes = codes or {}
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv, env=None):
        self.calls.append(list(argv))
        for needle, code in self.codes.items():
            if needle in " ".join(argv):
                return code
        return self.default

    @property
    def scripts_run(self) -> list[str]:
        return [os.path.basename(call[1]) for call in self.calls if len(call) > 1]

    def ran(self, script: str) -> bool:
        return script in self.scripts_run


class CombineTests(unittest.TestCase):
    """rc1 and rc2 -> rc, with 2 preserved."""

    def test_every_pair_of_codes(self):
        # 0 clean, 1 violation, 2 read/usage failure.
        table = {
            (0, 0): 0,
            (0, 1): 1,
            (0, 2): 2,
            (1, 0): 1,
            (1, 1): 1,
            (1, 2): 2,
            (2, 0): 2,
            (2, 1): 2,
            (2, 2): 2,
        }
        for rc1, rc2 in itertools.product((0, 1, 2), repeat=2):
            self.assertEqual(
                orch.combine(rc1, rc2),
                table[(rc1, rc2)],
                f"combine({rc1}, {rc2})",
            )

    def test_a_total_read_failure_is_not_reported_as_a_violation(self):
        """The invariant the format checker's docstring states, at the seam.

        "A failed read is never reported as a clean body" is promised by three
        checker docstrings and enforced by none of the composed path.
        """
        self.assertEqual(orch.combine(2, 0), orch.UNRUNNABLE)
        self.assertEqual(orch.combine(0, 2), orch.UNRUNNABLE)
        self.assertNotEqual(orch.combine(2, 0), orch.VIOLATION)

    def test_a_read_failure_outranks_a_violation(self):
        """The unread body is the larger unknown, so 2 wins over 1."""
        self.assertEqual(orch.combine(1, 2), 2)
        self.assertEqual(orch.combine(2, 1), 2)

    def test_combine_of_nothing_is_clean(self):
        self.assertEqual(orch.combine(), 0)

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_or_collapses_a_read_failure(self):
        """The old wiring, run: the defect was real, not remembered.

        `rc=$(( rc1 || rc2 ))` is the exact expression `action.yml` carried.
        Arithmetic `||` is a boolean operator, so it cannot produce 2 at all.
        """
        for rc1, rc2 in itertools.product((0, 1, 2), repeat=2):
            collapsed = subprocess.run(
                ["bash", "-c", f"rc1={rc1}; rc2={rc2}; echo $(( rc1 || rc2 ))"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            preserved = orch.combine(rc1, rc2)
            if preserved == orch.UNRUNNABLE:
                self.assertEqual(
                    collapsed,
                    "1",
                    f"the old bash reported {rc1}/{rc2} as {collapsed}, which "
                    "is the defect this module fixes",
                )
            else:
                self.assertEqual(int(collapsed), preserved)


class SelectModeTests(unittest.TestCase):
    def test_no_inputs_means_no_surface(self):
        self.assertEqual(orch.select_mode("", ""), "none")
        self.assertEqual(orch.select_mode(None, None), "none")
        self.assertEqual(orch.select_mode("  ", ""), "none")

    def test_a_pull_request_number_selects_the_body(self):
        self.assertEqual(orch.select_mode("1234", ""), "pr")
        self.assertEqual(orch.select_mode("1234", None), "pr")

    def test_a_comment_id_selects_the_comment(self):
        self.assertEqual(orch.select_mode("", "99"), "comment")
        self.assertEqual(orch.select_mode(None, "99"), "comment")

    def test_both_wired_together_is_a_usage_error(self):
        """Documented as mutually exclusive, and now refused before any fetch."""
        with self.assertRaises(orch.UsageError) as caught:
            orch.select_mode("1234", "99")
        message = str(caught.exception)
        self.assertIn("mutually exclusive", message)
        self.assertIn("1234", message)
        self.assertIn("99", message)


class ProseEscalationTests(unittest.TestCase):
    """A finding is worth what the repo said it was worth; a dead linter is not."""

    def test_clean_contributes_nothing(self):
        self.assertEqual(orch.escalate_prose(0, prose_fail=False), (0, None))
        self.assertEqual(orch.escalate_prose(0, prose_fail=True), (0, None))

    def test_a_finding_reports_without_blocking_by_default(self):
        self.assertEqual(orch.escalate_prose(1, prose_fail=False), (0, "warning"))

    def test_a_finding_blocks_when_the_repo_asked(self):
        self.assertEqual(orch.escalate_prose(1, prose_fail=True), (1, "error"))

    def test_a_linter_that_could_not_run_fails_whatever_prose_fail_says(self):
        for prose_fail in (False, True):
            rc, level = orch.escalate_prose(2, prose_fail=prose_fail)
            self.assertEqual(rc, 1, f"prose-fail={prose_fail}")
            self.assertEqual(level, "error")

    def test_anything_above_two_is_treated_as_could_not_run(self):
        self.assertEqual(orch.escalate_prose(3, prose_fail=False)[0], 1)


class LadderTests(unittest.TestCase):
    """Which checker runs, against which surface, in which order."""

    def _run_ladder(self, argv, recorder):
        original = orch._run
        orch._run = recorder
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = orch.main(argv)
        finally:
            orch._run = original
        return status, buffer.getvalue()

    def test_both_format_checkers_run_even_when_the_first_is_red(self):
        """One author, one push: a wrapped paragraph and a bad diagram together."""
        recorder = _Recorder({"format": 1, "mermaid": 0})
        status, _ = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "false"], recorder
        )
        self.assertEqual(status, 1)
        self.assertTrue(recorder.ran("check-pr-body-format.py"))
        self.assertTrue(recorder.ran("check-pr-body-mermaid.mjs"))

    def test_the_mermaid_checker_still_runs_when_the_format_checker_died(self):
        """A read failure (2) must not cancel the other checker."""
        recorder = _Recorder({"format": 2, "mermaid": 1})
        status, _ = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "false"], recorder
        )
        self.assertTrue(recorder.ran("check-pr-body-mermaid.mjs"))
        # 2 outranks 1, so the run reports the failure it could not recover from.
        self.assertEqual(status, 2)

    def test_comment_mode_skips_the_mermaid_checker(self):
        recorder = _Recorder()
        status, _ = self._run_ladder(["--repo", "o/r", "--comment", "99"], recorder)
        self.assertEqual(status, 0)
        self.assertTrue(recorder.ran("check-pr-body-format.py"))
        self.assertFalse(recorder.ran("check-pr-body-mermaid.mjs"))
        self.assertIn("--comment", recorder.calls[0])

    def test_comment_mode_says_that_prose_did_not_run(self):
        """`prose` defaults to true, so silence would be a lie by omission."""
        recorder = _Recorder()
        _, printed = self._run_ladder(["--repo", "o/r", "--comment", "99"], recorder)
        self.assertFalse(recorder.ran("check-pr-body-prose.py"))
        self.assertIn("prose check did not run", printed)
        self.assertIn("99", printed)

    def test_prose_runs_after_formatting_not_instead_of_it(self):
        recorder = _Recorder({"format": 1, "mermaid": 0, "prose": 1})
        status, _ = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "true", "--prose-fail", "false"],
            recorder,
        )
        self.assertEqual(
            recorder.scripts_run,
            ["check-pr-body-format.py", "check-pr-body-mermaid.mjs", "check-pr-body-prose.py"],
        )
        # Both findings in one run: red on formatting, green on the step overall
        # because the repo never asked a prose finding to block.
        self.assertEqual(status, 1)

    def test_prose_off_never_starts_the_prose_checker(self):
        recorder = _Recorder()
        status, printed = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "false"], recorder
        )
        self.assertFalse(recorder.ran("check-pr-body-prose.py"))
        self.assertIn("turned off", printed)
        self.assertEqual(status, 0)

    def test_a_reported_prose_finding_keeps_the_step_green(self):
        recorder = _Recorder({"prose": 1})
        status, printed = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "true", "--prose-fail", "false"],
            recorder,
        )
        self.assertEqual(status, 0)
        self.assertIn("::warning::", printed)

    def test_a_prose_finding_blocks_when_prose_fail_is_set(self):
        recorder = _Recorder({"prose": 1})
        status, printed = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "true", "--prose-fail", "true"],
            recorder,
        )
        self.assertEqual(status, 1)
        self.assertIn("::error::", printed)

    def test_a_linter_that_never_ran_fails_the_run(self):
        recorder = _Recorder({"prose": 2})
        status, printed = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "true", "--prose-fail", "false"],
            recorder,
        )
        self.assertEqual(status, 1)
        self.assertIn("could not run", printed)

    def test_an_unreadable_format_stays_a_read_failure_through_prose(self):
        """Format could not read the body (2), prose clean: still 2, never 1."""
        recorder = _Recorder({"format": 2, "mermaid": 0, "prose": 0})
        status, _ = self._run_ladder(
            ["--repo", "o/r", "--pr", "1234", "--prose", "true"], recorder
        )
        self.assertEqual(status, 2)


class UsageTests(unittest.TestCase):
    def test_wiring_both_surfaces_fails_loudly_and_reads_nothing(self):
        recorder = _Recorder()
        original = orch._run
        orch._run = recorder
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = orch.main(["--repo", "o/r", "--pr", "1234", "--comment", "99"])
        finally:
            orch._run = original
        self.assertEqual(status, orch.UNRUNNABLE)
        self.assertEqual(recorder.calls, [], "nothing should have been fetched")
        self.assertIn("::error::", buffer.getvalue())
        self.assertIn("mutually exclusive", buffer.getvalue())

    def test_a_non_numeric_pull_request_number_is_a_usage_error(self):
        recorder = _Recorder()
        original = orch._run
        orch._run = recorder
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = orch.main(["--repo", "o/r", "--pr", "not-a-number"])
        finally:
            orch._run = original
        self.assertEqual(status, orch.UNRUNNABLE)
        self.assertEqual(recorder.calls, [])
        self.assertIn("must be a number", buffer.getvalue())

    def test_an_event_with_neither_id_skips_green_and_says_why(self):
        recorder = _Recorder()
        original = orch._run
        orch._run = recorder
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = orch.main(["--repo", "o/r", "--pr", "", "--comment", ""])
        finally:
            orch._run = original
        self.assertEqual(status, 0)
        self.assertEqual(recorder.calls, [])
        self.assertIn("skipping", buffer.getvalue())

    def test_a_nonsense_prose_flag_is_a_usage_error(self):
        recorder = _Recorder()
        original = orch._run
        orch._run = recorder
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = orch.main(["--repo", "o/r", "--pr", "1", "--prose", "maybe"])
        finally:
            orch._run = original
        self.assertEqual(status, orch.UNRUNNABLE)
        self.assertIn("must be 'true' or 'false'", buffer.getvalue())


class OutputTests(unittest.TestCase):
    """Both outputs the action promises, and they are not the same number."""

    def _run_main(self, argv, recorder, output_path):
        original = orch._run
        orch._run = recorder
        try:
            with redirect_stdout(io.StringIO()):
                status = orch.main(argv + ["--github-output", output_path])
        finally:
            orch._run = original
        with open(output_path, encoding="utf-8") as handle:
            published = dict(
                line.split("=", 1) for line in handle.read().splitlines() if line
            )
        return status, published

    def test_a_reported_finding_publishes_prose_one_and_violations_zero(self):
        """`prose` is the verdict; `violations` is what the ladder failed on."""
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.txt")
            status, published = self._run_main(
                ["--repo", "o/r", "--pr", "1234", "--prose", "true", "--prose-fail", "false"],
                _Recorder({"prose": 1}),
                path,
            )
        self.assertEqual(status, 0)
        self.assertEqual(published["rc"], "0")
        self.assertEqual(published["prose_rc"], "1")

    def test_a_read_failure_publishes_two(self):
        """The value the `violations` output always documented."""
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.txt")
            status, published = self._run_main(
                ["--repo", "o/r", "--pr", "1234", "--prose", "false"],
                _Recorder({"format": 2, "mermaid": 0}),
                path,
            )
        self.assertEqual(status, 2)
        self.assertEqual(published["rc"], "2")

    def test_a_blocking_prose_finding_fails_the_step_but_not_violations(self):
        """Three numbers, three jobs: step conclusion, `violations`, `prose`.

        The prose step's own code is what the prose check found, but
        `violations` has always meant the format ladder's verdict. A repo that
        turns `prose-fail` on and has a badly formatted body should see `1` there
        for the formatting alone; a repo with a clean body and one prose
        finding should see `0` there and still get a red step.
        """
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.txt")
            status, published = self._run_main(
                ["--repo", "o/r", "--pr", "1234", "--prose", "true", "--prose-fail", "true"],
                _Recorder({"format": 0, "mermaid": 0, "prose": 1}),
                path,
            )
        self.assertEqual(status, 1, "the step must fail")
        self.assertEqual(published["rc"], "0", "the body had no formatting violation")
        self.assertEqual(published["prose_rc"], "1")

    def test_a_usage_error_publishes_two_on_both_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.txt")
            status, published = self._run_main(
                ["--repo", "o/r", "--pr", "1", "--comment", "2"], _Recorder(), path
            )
        self.assertEqual(status, 2)
        self.assertEqual(published["rc"], "2")
        self.assertEqual(published["prose_rc"], "2")

    def test_an_event_with_no_id_publishes_zero_on_both(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.txt")
            status, published = self._run_main(
                ["--repo", "o/r", "--pr", "", "--comment", ""], _Recorder(), path
            )
        self.assertEqual(status, 0)
        self.assertEqual(published["rc"], "0")
        self.assertEqual(published["prose_rc"], "0")

    def test_the_github_output_environment_variable_is_used_by_default(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.txt")
            original = orch._run
            orch._run = _Recorder()
            saved = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = path
            try:
                with redirect_stdout(io.StringIO()):
                    orch.main(["--repo", "o/r", "--comment", "7"])
            finally:
                orch._run = original
                if saved is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = saved
            with open(path, encoding="utf-8") as handle:
                self.assertIn("rc=0", handle.read())

    def test_running_by_hand_without_a_github_output_is_not_a_crash(self):
        original = orch._run
        orch._run = _Recorder()
        saved = os.environ.pop("GITHUB_OUTPUT", None)
        try:
            with redirect_stdout(io.StringIO()):
                status = orch.main(["--repo", "o/r", "--comment", "7"])
        finally:
            orch._run = original
            if saved is not None:
                os.environ["GITHUB_OUTPUT"] = saved
        self.assertEqual(status, 0)


class EntryPointTests(unittest.TestCase):
    def test_the_module_runs_as_a_script_and_reports_a_usage_error(self):
        """No network: both ids at once is refused before anything is read."""
        result = subprocess.run(
            [sys.executable, _SCRIPT, "--repo", "o/r", "--pr", "1", "--comment", "2"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stdout)

    def test_the_orchestrator_sits_beside_the_checkers_it_runs(self):
        """`action.yml` passes no path; the location is a property of the tree."""
        for checker in (
            orch._FORMAT_CHECKER,
            orch._MERMAID_CHECKER,
            orch._PROSE_CHECKER,
        ):
            self.assertTrue(
                os.path.isfile(os.path.join(orch._SCRIPTS_DIR, checker)),
                f"{checker} is not beside the orchestrator",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
