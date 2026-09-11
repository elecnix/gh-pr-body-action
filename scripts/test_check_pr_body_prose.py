#!/usr/bin/env python3
"""Unit tests for scripts/check-pr-body-prose.py.

Two of these are the reason the file exists.

`test_markdown_only_rule_is_caught_from_a_txt_body` closes the extension trap.
Vale picks its parser — and which config section applies — from the file
extension, so a body handed over as `body.txt` lints as plain text and every
markdown-scoped rule is skipped in silence. The test lints a body whose only
violation lives in a markdown heading, from a `.txt` source, and asserts the
violation is found. `test_control_a_txt_body_linted_directly_reports_nothing`
is its control: the same body, the same rules, linted without the copy, and
vale reports clean. Without the control the first test proves only that a rule
fires, not that the trap was real.

`test_repo_without_rules_is_skipped_and_says_why` is the other one. A repo that
ships no rules must be told it was skipped and why, never handed a silent pass.

Run: python3 scripts/test_check_pr_body_prose.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "check-pr-body-prose.py")

_spec = importlib.util.spec_from_file_location("check_pr_body_prose", _SCRIPT)
prose = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(prose)

_HAVE_VALE = shutil.which("vale") is not None

# A body whose only violation is inside a markdown heading. Linted as markdown
# the heading is a heading; linted as plain text it is a line starting with a
# hash, and a heading-scoped rule cannot see it.
BODY_WITH_HEADING_VIOLATION = "# A forbidden heading\n\nOrdinary prose sits here.\n"


def _write_rules(root: str) -> str:
    """A minimal, self-contained rule set: one markdown-scoped rule, no packages."""
    os.makedirs(os.path.join(root, "styles", "fixture"), exist_ok=True)
    config = os.path.join(root, ".vale.ini")
    with open(config, "w", encoding="utf-8") as handle:
        # MinAlertLevel matters: it silently hides everything below it, so a
        # warning-level rule under `error` would produce nothing and the test
        # would pass for the wrong reason.
        handle.write(
            textwrap.dedent(
                """\
                StylesPath = styles
                MinAlertLevel = warning

                [*.md]
                BasedOnStyles = fixture
                """
            )
        )
    with open(
        os.path.join(root, "styles", "fixture", "HeadingOnly.yml"), "w", encoding="utf-8"
    ) as handle:
        handle.write(
            textwrap.dedent(
                """\
                extends: existence
                message: "'%s' does not belong in a heading."
                level: warning
                scope: heading
                tokens:
                  - forbidden
                """
            )
        )
    return config


@unittest.skipUnless(_HAVE_VALE, "vale is not installed")
class ExtensionTrap(unittest.TestCase):
    def test_markdown_only_rule_is_caught_from_a_txt_body(self):
        with tempfile.TemporaryDirectory() as root:
            _write_rules(root)
            body_path = os.path.join(root, "body.txt")
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write(BODY_WITH_HEADING_VIOLATION)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = prose.main(["--body-file", body_path, "--rules-dir", root])

            self.assertEqual(status, 1, buffer.getvalue())
            self.assertIn("HeadingOnly", buffer.getvalue())

    def test_control_a_txt_body_linted_directly_reports_nothing(self):
        """The trap is real: without the copy, the same body reports clean."""
        with tempfile.TemporaryDirectory() as root:
            config = _write_rules(root)
            body_path = os.path.join(root, "body.txt")
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write(BODY_WITH_HEADING_VIOLATION)

            result = subprocess.run(
                ["vale", "--no-global", f"--config={config}", "--output=JSON", body_path],
                cwd=root,
                capture_output=True,
                text=True,
            )
            # Read the alerts, not the exit code: vale returns 0 on a warning
            # either way, so the exit code cannot tell the two cases apart.
            report = json.loads(result.stdout or "{}")
            alerts = [alert for group in report.values() for alert in group]
            self.assertEqual(alerts, [], result.stdout + result.stderr)

    def test_warning_alert_is_reported_although_vale_exits_zero(self):
        """The other silent-pass trap: vale exits 0 for every warning.

        The fixture rule is `level: warning`, so vale's own status is 0 on the
        markdown copy as well. If this checker read the status instead of the
        report, the violating body above would pass.
        """
        with tempfile.TemporaryDirectory() as root:
            config = _write_rules(root)
            body_path = os.path.join(root, "body.md")
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write(BODY_WITH_HEADING_VIOLATION)
            result = subprocess.run(
                ["vale", "--no-global", f"--config={config}", "--output=JSON", body_path],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, "vale is expected to exit 0 here")
            self.assertIn("HeadingOnly", result.stdout)

            status, report = prose.lint(BODY_WITH_HEADING_VIOLATION, config, root)
            self.assertEqual(status, 1, report)
            self.assertIn("HeadingOnly", report)

    def test_stdin_body_is_linted_as_markdown_too(self):
        """Stdin has no extension at all, so it is the trap's worst case."""
        with tempfile.TemporaryDirectory() as root:
            _write_rules(root)
            original = sys.stdin
            sys.stdin = io.StringIO(BODY_WITH_HEADING_VIOLATION)
            buffer = io.StringIO()
            try:
                with redirect_stdout(buffer):
                    status = prose.main(["--body-file", "-", "--rules-dir", root])
            finally:
                sys.stdin = original
            self.assertEqual(status, 1, buffer.getvalue())
            self.assertIn("HeadingOnly", buffer.getvalue())

    def test_clean_body_passes(self):
        with tempfile.TemporaryDirectory() as root:
            _write_rules(root)
            body_path = os.path.join(root, "body.txt")
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write("# An allowed heading\n\nOrdinary prose sits here.\n")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = prose.main(["--body-file", body_path, "--rules-dir", root])
            self.assertEqual(status, 0, buffer.getvalue())
            self.assertIn("clean", buffer.getvalue())


class NoRules(unittest.TestCase):
    def test_repo_without_rules_is_skipped_and_says_why(self):
        with tempfile.TemporaryDirectory() as root:
            body_path = os.path.join(root, "body.md")
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write("Anything at all.\n")

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = prose.main(["--body-file", body_path, "--rules-dir", root])

            printed = buffer.getvalue()
            self.assertEqual(status, 0)
            self.assertIn("skipped", printed)
            self.assertIn(".vale.ini", printed)
            self.assertIn("carries no rules of its own", printed)

    def test_find_config_returns_none_without_a_config(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(prose.find_config(root, None))

    def test_find_config_finds_the_repo_config(self):
        with tempfile.TemporaryDirectory() as root:
            config = _write_rules(root)
            self.assertEqual(prose.find_config(root, None), config)


class EmptyBody(unittest.TestCase):
    def test_empty_body_is_not_a_violation(self):
        with tempfile.TemporaryDirectory() as root:
            _write_rules(root)
            body_path = os.path.join(root, "body.md")
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write("   \n")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = prose.main(["--body-file", body_path, "--rules-dir", root])
            self.assertEqual(status, 0)
            self.assertIn("nothing to lint", buffer.getvalue())


class Defang(unittest.TestCase):
    def test_workflow_command_in_echoed_output_is_defanged(self):
        self.assertNotEqual(prose._defang("::error::boom"), "::error::boom")
        self.assertEqual(prose._defang("ordinary line"), "ordinary line")


if __name__ == "__main__":
    unittest.main(verbosity=2)
