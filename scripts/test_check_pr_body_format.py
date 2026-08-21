#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for scripts/check-pr-body-format.py.

Every fixture is synthetic, and each bug shape is paired with the clean shape it
is easiest to confuse it with — that pairing is the point, because the way this
check fails is by reddening correct bodies, not by missing broken ones.

Most of the false-positive cases are not hypothetical: they were measured against
the 100 most recent PR bodies, so a future loosening of a pattern re-breaks a test
rather than a merge. The few that are latent rather than measured say so in their
own docstring.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "check-pr-body-format.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_pr_body_format", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_pr_body_format"] = mod
    spec.loader.exec_module(mod)
    return mod


_CHECKER = _load()
find_violations = _CHECKER.find_violations
Violation = _CHECKER.Violation


def _patterns(body: str) -> set[str]:
    return {v.pattern for v in find_violations(body)}


class TestParagraphHardBreak(unittest.TestCase):
    def test_clean_paragraphs_separated_by_blank_line(self):
        body = (
            "This is paragraph one. It has a sentence.\n"
            "\n"
            "This is paragraph two.\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_clean_source_wrapped_sentence(self):
        # A source-wrapped sentence: line 1 ends with a period, line 2 starts
        # with a capital. Renders as one paragraph; the conservative heuristic
        # does NOT flag it.
        body = (
            "This is a long sentence that ends here.\n"
            "The next sentence starts on the next line.\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_flag_two_prose_lines_jammed_no_blank(self):
        # The bug shape: line 1 does not end a sentence, line 2 starts lowercase
        # — reads as a wall of text / mid-sentence break.
        body = (
            "This is a line that does not end with a period\n"
            "and the next line continues it with no blank line between them\n"
        )
        patterns = _patterns(body)
        self.assertIn("hard-newline-in-paragraph", patterns)

    def test_heading_then_prose_not_flagged(self):
        body = "## Heading\n\nProse under the heading.\n"
        self.assertEqual(_patterns(body), set())

    def test_prose_then_list_not_flagged(self):
        body = "Intro paragraph.\n\n- item one\n- item two\n"
        self.assertEqual(_patterns(body), set())


class TestListItemHardBreak(unittest.TestCase):
    def test_clean_list_with_indented_continuation(self):
        body = (
            "- item one\n"
            "  continuation of item one\n"
            "- item two\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_clean_list_items_separated(self):
        body = "- item one\n- item two\n- item three\n"
        self.assertEqual(_patterns(body), set())

    def test_flag_list_item_followed_by_column_zero_prose(self):
        # The bug shape: a `- item` line immediately followed by a prose line
        # at column zero. Lazy continuation keeps that line inside the bullet,
        # and comment mode hard-breaks it, so the item renders as two ragged
        # lines rather than one flowing line.
        body = (
            "- This is a list item that continues\n"
            "on the next line at column zero\n"
        )
        patterns = _patterns(body)
        self.assertIn("hard-newline-in-list-item", patterns)

    def test_ordered_list_item_followed_by_prose_flagged(self):
        body = (
            "1. First item that wraps\n"
            "and continues at column zero\n"
        )
        patterns = _patterns(body)
        self.assertIn("hard-newline-in-list-item", patterns)

    def test_list_then_blank_then_prose_not_flagged(self):
        body = "- item one\n\nProse after the list.\n"
        self.assertEqual(_patterns(body), set())


class TestCollapsedTable(unittest.TestCase):
    def test_clean_multiline_table(self):
        body = (
            "| Col A | Col B |\n"
            "| --- | --- |\n"
            "| a1 | b1 |\n"
            "| a2 | b2 |\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_flag_header_separator_data_on_one_line(self):
        # The bug shape: header + separator + data row all on one line.
        body = "| Col A | Col B | --- | --- | a1 | b1 |\n"
        patterns = _patterns(body)
        self.assertIn("collapsed-table", patterns)

    def test_flag_separator_then_data_on_same_line(self):
        # Separator row followed immediately by a data row's pipes, no newline.
        body = "| Col A | Col B |\n| --- | --- | a1 | b1 | a2 | b2 |\n"
        patterns = _patterns(body)
        self.assertIn("collapsed-table", patterns)

    def test_table_inside_code_fence_not_flagged(self):
        # A fenced code block's pipes are not a rendered table.
        body = (
            "```\n"
            "| Col A | Col B | --- | --- | a1 | b1 |\n"
            "```\n"
        )
        self.assertEqual(_patterns(body), set())


class TestMandatedSignatureFooter(unittest.TestCase):
    """The `— <handle>` footer AGENTS.md mandates must never be flagged.

    Measured against the last 100 PRs: this shape appears on nearly every
    AI-authored body, and an earlier draft flagged it as a paragraph hard
    break (the em dash reads as a continuation marker, and the `Linked:` line
    above it ends with `)` rather than sentence punctuation). A gate that
    fires on the footer it requires gets itself switched off within a day.
    """

    def test_linked_line_then_signature_not_flagged(self):
        body = (
            "Some closing prose.\n"
            "\n"
            "Linked: [PRI-1763](https://linear.app/prizmal/issue/PRI-1763/a-slug)\n"
            "— crimson-lemur-13\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_signature_with_trailing_emoji_not_flagged(self):
        body = (
            "Linked: [PRI-1834](https://linear.app/prizmal/issue/PRI-1834)\n"
            "— cc-swift-viper-85 \U0001f916\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_em_dash_aside_is_still_flagged(self):
        # An em-dash continuation that is NOT the handle footer is a genuine
        # mid-sentence wrap and must still be caught.
        body = (
            "The router resolves the target per request\n"
            "— which is why the signal has to precede it in the pipeline\n"
        )
        self.assertIn("hard-newline-in-paragraph", _patterns(body))


class TestAlignedTableSeparator(unittest.TestCase):
    """A well-formed separator row with alignment colons is not a collapsed table.

    Measured false positive: `| -- | -- | --: | --: |` on its own line. The
    colon in a right-aligned cell counted as data content, so the row looked
    like a separator followed by a data row.
    """

    def test_right_aligned_separator_row_not_flagged(self):
        body = (
            "| Gate | Result | Cost | Delta |\n"
            "| -- | -- | --: | --: |\n"
            "| a | b | 1 | 2 |\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_all_alignment_styles_not_flagged(self):
        body = (
            "| A | B | C |\n"
            "| :-- | :--: | --: |\n"
            "| 1 | 2 | 3 |\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_collapsed_table_with_aligned_separator_still_flagged(self):
        body = "| A | B |\n| :-- | --: | a1 | b1 |\n"
        self.assertIn("collapsed-table", _patterns(body))


class TestIndentedAndNestedListItems(unittest.TestCase):
    """Indented list items are list items, not prose.

    Measured false positive: a block of ` - bullet;` lines indented by one
    space was classified as prose, so every adjacent pair was reported as a
    paragraph hard break.
    """

    def test_indented_bullets_not_flagged(self):
        body = (
            "Intro:\n"
            "\n"
            "  - never-configured, the keychain service and the setup command;\n"
            "  - keychain item exists but the value is unreadable;\n"
            "  - key file exists but is unreadable;\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_nested_bullets_not_flagged(self):
        body = (
            "- top level item\n"
            "  - nested item one\n"
            "  - nested item two\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_indented_ordered_items_not_flagged(self):
        body = "  1. first\n  2. second\n"
        self.assertEqual(_patterns(body), set())


class TestBotAuthoredBodiesAreSkipped(unittest.TestCase):
    """A machine-generated body is out of scope — nobody can hand-fix it.

    Dependabot bodies are multi-hundred-line raw HTML changelog dumps. They are
    not a review surface a human wrote, so the gate reports them as skipped
    rather than red.
    """

    def test_bot_author_short_circuits_to_zero(self):
        calls = []

        def fake_fetch(repo, pr):
            calls.append((repo, pr))
            return ("- item\ncontinuation at column zero\n", True)

        original = _CHECKER._fetch_body
        _CHECKER._fetch_body = fake_fetch
        try:
            rc = _CHECKER.main(["--repo", "o/r", "--pr", "7"])
        finally:
            _CHECKER._fetch_body = original
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("o/r", 7)])

    def test_human_author_with_violation_still_red(self):
        def fake_fetch(repo, pr):
            return ("- item\ncontinuation at column zero\n", False)

        original = _CHECKER._fetch_body
        _CHECKER._fetch_body = fake_fetch
        try:
            rc = _CHECKER.main(["--repo", "o/r", "--pr", "7"])
        finally:
            _CHECKER._fetch_body = original
        self.assertEqual(rc, 1)

    def test_failed_read_is_degraded_not_clean(self):
        # A failed API read must never be reported as "no violations".
        def fake_fetch(repo, pr):
            raise RuntimeError("gh api failed (1): boom")

        original = _CHECKER._fetch_body
        _CHECKER._fetch_body = fake_fetch
        try:
            rc = _CHECKER.main(["--repo", "o/r", "--pr", "7"])
        finally:
            _CHECKER._fetch_body = original
        self.assertEqual(rc, 2)


class TestStructuralLinesThatLookLikeProse(unittest.TestCase):
    """Shapes that head a paragraph rather than continuing one.

    None of these occur in the calibration corpus, so they are latent rather
    than measured — but the bold pseudo-heading is what a "write for humans
    first" body reaches for constantly, so it was going to arrive.
    """

    def test_bold_pseudo_heading_not_flagged(self):
        body = (
            "**Fail-open by design.**\n"
            "the leg still proceeds when the reserve call times out.\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_bold_heading_with_colon_not_flagged(self):
        body = "**Scope separation is pinned by tests:**\nsee the authz suite.\n"
        self.assertEqual(_patterns(body), set())

    def test_paragraph_opening_with_bold_is_still_prose(self):
        # Bold at the START of a line that continues in plain text is an
        # ordinary paragraph, so a real wrap in it must still be caught.
        body = (
            "**Fail-open by design** — the deliberate opposite of auth's\n"
            "fail-closed posture, which matters on every billable leg.\n"
        )
        self.assertIn("hard-newline-in-paragraph", _patterns(body))

    def test_setext_underline_not_flagged(self):
        body = "A heading in setext form\n========================\n"
        self.assertEqual(_patterns(body), set())

    def test_horizontal_rule_not_flagged(self):
        body = "Some closing prose\n---\n"
        self.assertEqual(_patterns(body), set())

    def test_placeholder_dashes_not_flagged(self):
        body = "Run it with the flag\n--force-with-lease is required.\n"
        self.assertEqual(_patterns(body), set())


class TestRunsCollapseIntoOneViolation(unittest.TestCase):
    """A hard-wrapped paragraph is one defect with one fix, so it is one finding.

    Reporting every adjacent pair turned a single wrapped body into 128 findings
    and a 25 KB run log that buried every other pattern.
    """

    def test_wrapped_run_is_a_single_violation(self):
        body = (
            "this is a wrapped paragraph that keeps going\n"
            "and going across another line without stopping\n"
            "and still going across a third line here\n"
            "and finally reaching the end of the run.\n"
        )
        violations = find_violations(body)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].start, 1)
        self.assertEqual(violations[0].end, 4)

    def test_two_separate_runs_are_two_violations(self):
        body = (
            "first wrapped line that runs on\n"
            "and continues here.\n"
            "\n"
            "## A heading\n"
            "\n"
            "second wrapped line that runs on\n"
            "and continues here too.\n"
        )
        self.assertEqual(len(find_violations(body)), 2)

    def test_long_run_truncates_its_echoed_lines(self):
        body = "".join(f"wrapped line number {n} continuing on\n" for n in range(20))
        rendered = find_violations(body)[0].render()
        self.assertIn("more line(s) in the same run", rendered)
        # Head line + the echo cap + the summary line.
        self.assertLessEqual(
            len(rendered.splitlines()), _CHECKER._MAX_ECHOED_LINES + 2
        )


class TestWorkflowCommandInjection(unittest.TestCase):
    """A PR body is attacker-influenceable on a fork PR.

    The Actions runner executes any stdout line whose first non-space characters
    are `::`, and it strips leading whitespace before deciding — so indenting the
    echoed line is not enough on its own.
    """

    def test_leading_workflow_command_is_defanged(self):
        self.assertEqual(_CHECKER._defang("::error::boom"), "'::error::boom")

    def test_indented_workflow_command_is_defanged(self):
        self.assertEqual(
            _CHECKER._defang("   ::stop-commands::tok"), "   '::stop-commands::tok"
        )

    def test_ordinary_line_is_untouched(self):
        self.assertEqual(_CHECKER._defang("a normal line"), "a normal line")

    def test_mid_line_colons_are_untouched(self):
        self.assertEqual(_CHECKER._defang("see foo::bar"), "see foo::bar")

    def test_rendered_violation_never_starts_a_command(self):
        body = "::error::this line wraps\nand continues down here\n"
        for violation in find_violations(body):
            for line in violation.render().splitlines():
                self.assertFalse(line.lstrip().startswith("::"))


class TestHtmlCommentMasking(unittest.TestCase):
    def test_codesmith_footer_not_flagged(self):
        # The codesmith footer block carries HTML with pipes inside comments and
        # `<a href>` tags. It must not trip the table or prose detectors.
        body = (
            "## What\n\nReal prose here.\n\n"
            "<!-- codesmith:footer -->\n"
            "<a href=\"https://app.blacksmith.sh/x/y/z\"><picture>"
            "<source media=\"(prefers-color-scheme: dark)\" srcset=\"u\">"
            "</picture></a>\n"
            "<!-- /codesmith:footer -->\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_multiline_html_comment_masked(self):
        body = (
            "<!-- a comment that\n"
            "spans two lines and has | pipes | --- | inside -->\n"
            "\n"
            "Prose.\n"
        )
        self.assertEqual(_patterns(body), set())


class TestWholeBodyIsAFileReference(unittest.TestCase):
    """A body that is only `@/tmp/pr-body.md` is a paste accident, not a body.

    An agent (or a human scripting one) writes the intended body to a temp
    file and then sets the PR body to the *reference* `@/tmp/pr-body.md`,
    expecting the tooling to expand it. Nothing expands it, so the PR reaches
    review with a one-token body that renders as literal text. The whole body
    must be the reference — a mention or a path inside a real body is prose
    and is not touched.
    """

    def test_flag_absolute_tmp_path(self):
        body = "@/tmp/pr-body.md\n"
        patterns = _patterns(body)
        self.assertIn("body-is-file-reference", patterns)

    def test_flag_relative_path(self):
        body = "@./pr-body.md"
        self.assertIn("body-is-file-reference", _patterns(body))

    def test_flag_bare_filename(self):
        body = "@pr-body.md"
        self.assertIn("body-is-file-reference", _patterns(body))

    def test_flag_home_relative_path(self):
        body = "@~/notes.txt"
        self.assertIn("body-is-file-reference", _patterns(body))

    def test_mention_only_body_not_flagged(self):
        # `@username` is a (useless but valid) mention, not a path — no slash,
        # no extension. Latent rather than measured.
        self.assertEqual(_patterns("@username\n"), set())

    def test_team_mention_only_body_not_flagged(self):
        # `@org/team` has a slash but no file extension; it is a mention.
        self.assertEqual(_patterns("@org/team\n"), set())

    def test_mention_inside_a_real_body_not_flagged(self):
        body = (
            "Thanks @reviewer for the context.\n"
            "\n"
            "The fix lands in the next push.\n"
        )
        self.assertEqual(_patterns(body), set())

    def test_reference_plus_real_content_not_flagged(self):
        # Only a body that is *solely* the reference is a paste accident.
        body = "@/tmp/pr-body.md\n\nReal content under it.\n"
        self.assertEqual(_patterns(body), set())

    def test_violation_spans_the_whole_body(self):
        violations = find_violations("@/tmp/pr-body.md\n")
        self.assertEqual(len(violations), 1)
        self.assertEqual((violations[0].start, violations[0].end), (1, 1))


class TestEmptyBody(unittest.TestCase):
    def test_empty_body_clean(self):
        self.assertEqual(find_violations(""), [])

    def test_whitespace_only_clean(self):
        self.assertEqual(find_violations("   \n\n  \n"), [])


class TestExitCodes(unittest.TestCase):
    def test_main_clean_body_returns_zero(self):
        import tempfile, os as _os

        d = _os.path.dirname(_os.path.abspath(__file__))
        script = _os.path.join(d, "check-pr-body-format.py")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("Clean paragraph.\n\nAnother paragraph.\n")
            path = f.name
        try:
            rc = _CHECKER.main(["--body-file", path])
            self.assertEqual(rc, 0)
        finally:
            _os.unlink(path)

    def test_main_violation_body_returns_one(self):
        import tempfile, os as _os

        d = _os.path.dirname(_os.path.abspath(__file__))
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(
                "- list item that wraps\n"
                "and continues at column zero\n"
            )
            path = f.name
        try:
            rc = _CHECKER.main(["--body-file", path])
            self.assertEqual(rc, 1)
        finally:
            _os.unlink(path)

    def test_main_mutually_exclusive_args(self):
        with self.assertRaises(SystemExit) as cm:
            _CHECKER.main(["--body-file", "x", "--repo", "o/r", "--pr", "1"])
        self.assertEqual(cm.exception.code, 2)

    def test_main_missing_args(self):
        with self.assertRaises(SystemExit) as cm:
            _CHECKER.main([])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()