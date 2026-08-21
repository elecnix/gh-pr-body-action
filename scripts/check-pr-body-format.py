#!/usr/bin/env python3
"""Flag a PR description that will render badly, before it reaches review.

This docstring is the canonical write-up of the mechanic behind the gate.
The repo README.md carries the usage entry.

GitHub renders a PR body in comment mode, where a single newline becomes a
`<br>`. A body wrapped at 80 columns in an editor therefore renders on GitHub as
a ragged column of broken lines, and a table whose rows lost their newlines
renders as one run of text. The author sees neither, because the editor shows
the source.

Verified rather than assumed, and the contrast is the load-bearing half:

    POST /markdown  mode=gfm       "one line\\nsecond line"
        -> "<p>one line<br>\\nsecond line</p>"     # a PR body, a comment
    POST /markdown  mode=markdown  "one line\\nsecond line"
        -> "<p>one line\\nsecond line</p>"         # a committed .md file

Same input, two surfaces, two results. That is why the wrapped markdown files
throughout this repo are correct exactly as they are, and why identical wrapping
in a PR body is a defect: only the comment surface hard-breaks. The rule is about
what you type into GitHub, not about what you commit — which is also why it does
not belong in AGENTS.md's "Write for humans first". Style governs how you
express a thing; this is a rendering mechanic of one surface.

Three shapes are flagged:

  1. **Hard newline inside a paragraph** — a prose line that stops mid-sentence
     and continues on the next line. This is the common one: a whole body
     hard-wrapped at a column limit.

         Bad:   The router resolves the target per request, which is
                why the signal has to precede it.
         Renders as two lines with a `<br>` between them.
         Fix:   let the line run long; the browser wraps it.

  2. **Hard newline inside a list item** — a `- item` line followed by prose at
     column zero. Markdown's lazy continuation keeps that line inside the
     bullet, so the text is not lost; comment mode simply hard-breaks it, and
     the item renders as two ragged lines instead of one flowing line.

         Bad:   - the reserve id is minted per billable leg
                and stashed for the settle worker
         Renders as: <li>…billable leg<br>and stashed…</li>
         Fix:   indent the continuation by two spaces.

  3. **Collapsed table** — header, separator, and data rows pipe-joined onto one
     line, which renders as text rather than a table.

         Bad:   | Gate | Result | --- | --- | gitleaks | clean |
         Renders as a run of pipes and words, not a table.
         Fix:   one row per line.

Calibrated against the 100 most recent PR bodies (read via the REST list
endpoint, 2026-08-13), of which 94 are human-authored and 6 are bot-authored and
skipped. Shape 1 is real and common: 11 of the 94 are hard-wrapped. Shapes 2 and
3 do not occur in that window; they are kept because they are cheap, tested, and
are the two failure modes that destroy a body outright rather than merely making
it ragged. The calibration set is described in scripts/check-pr-body-format.py.

Seven shapes are deliberately NOT flagged. The first four each fired on a real
body in an earlier draft of this check; the last three are latent — zero
occurrences in the corpus, suppressed because the house style produces them. All
seven are pinned by a test:

  * the `— <handle>` signature footer AGENTS.md mandates (it renders as its own
    line, which is the intent);
  * a separator row carrying alignment colons, `| -- | --: |` — the colon is
    part of the cell, not evidence of a data row;
  * indented and nested list items — a bullet is a bullet at any indent;
  * bot-authored bodies, which are machine-generated HTML nobody can hand-fix;
  * a whole-line bold pseudo-heading, `**Like this.**`, which heads the
    paragraph under it, so the newline after it is intended;
  * a setext underline or a horizontal rule, which is structural rather than a
    prose line that happens to open with a dash;
  * a line opening with `--`, which is a CLI flag or a placeholder rather than a
    sentence continuing.

Why this is hand-written rather than an off-the-shelf linter, measured rather
than assumed. A general markdown linter cannot detect this defect, because in a
markdown *file* a hard-wrapped paragraph is correct — which is why the wrapped
files across this repo are fine. Run PyMarkdownLnt (a port of the markdownlint
rule set, 44 rules) over a body carrying all three shapes and it reports none of
them. Run it over a *correct* PR body and it reports 52 findings, 37 of them
`MD013 line-length` — the rule that tells you to wrap at 80 columns, which is the
defect. The linter is not merely silent here; it recommends the bug.

Two alternatives were evaluated and rejected, both against the live API:

  * **GitHub's renderer as the oracle** — render the body and flag the breaks.
    It does not remove the hard part. The mandated `— <handle>` footer renders
    with a `<br>` too, so the renderer says where GitHub breaks but never which
    breaks were intended, and that judgement is every suppression above.
    Diffing `mode=gfm` against `mode=markdown` is worse still: a bare PR
    reference and an `@user` expand to hovercard anchors in one mode and stay
    text in the other, so nearly every body in this repo differs for reasons
    that are not defects.
    It would also cost an API call per check and a token, ending the offline
    `--body-file` path.
  * **A real parser (markdown-it-py) instead of the line classifier** — the
    genuinely attractive one, and the closest call. It yields `softbreak` tokens
    directly, so shapes 1 and 2 need no heuristics. But it does not see the
    collapsed table as a table either (that stays an inference), it flags the
    mandated footer exactly the same way, and it would put a third-party
    dependency in a CI gate that parses attacker-influenceable PR text while the
    repo's other Python gates run on the standard library. The regex classifier
    measured zero false positives over 100 real bodies, so the accuracy it would
    buy is not currently missing. Revisit if a misclassification ever does
    surface: swapping the classifier is contained, since the judgement layer
    above sits on top of it either way.

This check is **advisory** at the branch-protection ruleset level, and that is a
deliberate choice rather than a step toward required: on the calibration corpus
it still flags 11 of the 94 human-authored bodies, all of them already merged.
Making it required would redden the trunk for bodies nobody is going to rewrite.

Advisory here means the check goes **red** without blocking the merge — not that
it may be skipped. The `/pr` skill's rule is that every advisory finding gets
read and then either acted on (fixed here, or filed as a follow-up) or dismissed
with a stated reason. Nothing mechanically enforces that, so the honest claim is
"must be read", not "blocks the merge".

Usage — CI reads the PR body from the GitHub API:

    python3 scripts/check-pr-body-format.py --repo PrizmalAi/PrizmalSwitch --pr 1234

Local repro against a saved body (deterministic, no network):

    python3 scripts/check-pr-body-format.py --body-file body.md
    cat body.md | python3 scripts/check-pr-body-format.py --body-file -

Exit codes:
  0  no violations, the body is empty, or the author is a bot.
  1  at least one violation; each is printed with the pattern name, the line
     range, and the offending line(s).
  2  invalid usage, the GitHub API call failed, or the body could not be read.
     A failed read is never reported as a clean body.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass


# A line is "structural" when a renderer treats it as anything other than a
# paragraph of prose: a heading, a list item, a table row, a blockquote, a code
# fence, raw HTML, or blank. The check only ever looks at prose-vs-prose and
# list-vs-prose adjacency, so classifying the rest is enough.
_HEADING = re.compile(r"^#{1,6}\s")
# A setext underline (`====` / `----`) and a horizontal rule are structural, not
# a prose line starting with a dash.
_RULE = re.compile(r"^\s*(={2,}|-{2,}|\*{3,}|_{3,})\s*$")
# A whole line that is nothing but bold text, optionally closed with `.` or `:`
# — the pseudo-heading a "write for humans first" body reaches for constantly.
# It heads the paragraph under it, so the newline after it is intended.
_BOLD_HEADING = re.compile(r"^\s*\*\*[^*].*\*\*[.:]?\s*$")
# Leading whitespace is allowed: a nested or indented list item is still a list
# item. Reading `  - foo` as prose made every indented bullet block look like a
# run of jammed paragraph lines.
_OLIST = re.compile(r"^\s*\d{1,9}[.)]\s")
_ULIST = re.compile(r"^\s*[-*+]\s")
_TABLE = re.compile(r"^\s*\|")
_QUOTE = re.compile(r"^>\s?")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HTML = re.compile(r"^\s*<")
# One cell of a table separator row: `---`, `:--`, `--:`, `:-:`.
_SEP_CELL = re.compile(r"^:?-{2,}:?$")
# Split a table row on its unescaped pipes; `\|` inside a cell is literal text.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
# The agent signature footer AGENTS.md mandates: an em dash, the kebab-case
# session handle, and an optional trailing emoji. It sits directly under the
# `Linked:` line by convention, and that adjacency is intended — the `<br>`
# between them is what puts the handle on its own line.
_SIGNATURE = re.compile(r"^\s*[—–]\s*[a-z][a-z0-9]*(?:-[a-z0-9]+)+\s*\S?\s*$")
# A line that is only a close-tag or an HTML comment marker — counted as HTML.
_HTML_COMMENT = re.compile(r"^\s*<!--")
# Sentence-ending punctuation, allowing a closing quote/bracket after it. Used
# by the paragraph-break heuristic to suppress the common false positive of a
# source-wrapped sentence (line 1 ends with `.`, line 2 starts with a capital).
_SENTENCE_END = re.compile(r"[.!?][\"'\)\]`]*\s*$")
# A line that starts with a lowercase letter, a digit, or a continuation
# punctuation mark (`:`, `;`, `,`, or a dash that is em/en, not hyphen-minus) —
# strong evidence the prior line was wrapped mid-sentence rather than a
# paragraph break. Hyphen-minus is excluded on purpose: a line opening with one
# is a CLI flag, a `--` placeholder, or a rule, never a sentence continuing.
_CONTINUATION_START = re.compile(r"^\s*[a-z0-9:;,—–]")


# How many offending lines to print before summarizing the rest. A hard-wrapped
# body is one defect with one fix, so echoing all 300 of its lines back buys the
# author nothing and buries every other finding in the run log.
_MAX_ECHOED_LINES = 4
# How many violations to print in full. A body wrapped end to end makes every
# paragraph a finding, and past the first handful the reader has the message.
_MAX_REPORTED = 15


def _defang(line: str) -> str:
    """Neutralize a line the Actions runner would read as a workflow command.

    A PR body is attacker-influenceable on a fork PR, and the runner parses any
    stdout line whose first non-space characters are `::` — so a body line of
    `::error::` or `::stop-commands::<tok>` would otherwise be executed as a
    command rather than printed as evidence. Indenting is not enough: the runner
    strips leading whitespace before it looks. Today the blast radius is small
    (a `pull_request` trigger, a read-only token, no secrets in the job), so this
    buys spoofed annotations only — but it stops being small the first time this
    workflow gains a write scope, and it costs one substitution.
    """
    if line.lstrip().startswith("::"):
        return line.replace("::", "'::", 1)
    return line


@dataclass(frozen=True)
class Violation:
    pattern: str
    start: int  # 1-indexed, inclusive
    end: int  # 1-indexed, inclusive
    lines: tuple[str, ...]

    def render(self) -> str:
        head = f"{self.pattern} (lines {self.start}–{self.end})"
        shown = [f"    {_defang(ln)}" for ln in self.lines[:_MAX_ECHOED_LINES]]
        hidden = len(self.lines) - len(shown)
        if hidden > 0:
            shown.append(f"    … {hidden} more line(s) in the same run")
        return "\n".join([head, *shown])


def _classify(line: str) -> str:
    """One of: blank, heading, ulist, olist, table, quote, fence, html,
    signature, prose."""
    if not line.strip():
        return "blank"
    if _HEADING.match(line):
        return "heading"
    if _RULE.match(line):
        return "heading"
    if _BOLD_HEADING.match(line):
        return "heading"
    if _TABLE.match(line):
        return "table"
    if _SIGNATURE.match(line):
        return "signature"
    if _ULIST.match(line):
        return "ulist"
    if _OLIST.match(line):
        return "olist"
    if _QUOTE.match(line):
        return "quote"
    if _FENCE.match(line):
        return "fence"
    if _HTML.match(line):
        # `_HTML` already covers an HTML comment — `_strip_masked` has blanked
        # those before this runs anyway, so there is no separate comment arm.
        return "html"
    return "prose"


def _strip_masked(lines: list[str]) -> list[str]:
    """Drop fenced code blocks and HTML comments so their pipes/prose are ignored.

    A code fence's pipes and a `<!-- codesmith:footer -->` block's HTML are
    structural in the rendered body but not in the raw text, so a detector that
    reads the raw lines would flag them. Masking replaces them with blank lines
    so the line numbering (which the violations report) stays exact.
    """
    out: list[str] = []
    in_fence = False
    in_comment = False
    for line in lines:
        if in_fence:
            if _FENCE.match(line):
                in_fence = False
                out.append("")
            else:
                out.append("")
            continue
        if in_comment:
            # A comment ends on a line containing `-->`.
            if "-->" in line:
                in_comment = False
            out.append("")
            continue
        if _FENCE.match(line):
            in_fence = True
            out.append("")
            continue
        if _HTML_COMMENT.match(line):
            # A comment may span one or many lines.
            if "-->" not in line:
                in_comment = True
            out.append("")
            continue
        out.append(line)
    return out


def _is_paragraph_hard_break(prev: str, cur: str) -> bool:
    """Two adjacent prose lines with no blank line between them.

    GitHub renders two consecutive non-blank prose lines as a single paragraph
    with a soft break (a `<br>` on a trailing-two-space line, or a joined line
    otherwise). The bug the operator calls out is two prose lines jammed together
    that read as a wall of text or a mid-sentence break — i.e. line 1 does NOT
    end a sentence and line 2 reads as a continuation of line 1.

    The conservative heuristic: flag only when line 1 does NOT end with sentence
    punctuation AND line 2 starts with a continuation marker (lowercase, digit,
    or a continuation punctuation). This suppresses the legitimate case of a
    source-wrapped sentence where the author split a long line at a sentence
    boundary (`... done.` on line 1, `The next sentence...` on line 2 with a
    capital) — that renders as one paragraph and is fine.
    """
    if _SENTENCE_END.search(prev.strip()):
        return False
    if not _CONTINUATION_START.match(cur):
        return False
    return True


def _find_paragraph_hard_breaks(lines: list[str]) -> list[Violation]:
    """One violation per wrapped *run*, not per adjacent pair.

    A hard-wrapped paragraph is a single defect with a single fix, but every
    consecutive pair inside it satisfies the rule. Reporting each pair turned one
    wrapped body into 128 findings and a 25 KB run log that buried the other
    patterns, so a run of broken lines collapses into one violation spanning it.
    """
    out: list[Violation] = []
    run_start: int | None = None

    def close(run_end: int) -> None:
        # `run_end` is the index of the last line in the run, inclusive.
        assert run_start is not None
        out.append(
            Violation(
                pattern="hard-newline-in-paragraph",
                start=run_start + 1,
                end=run_end + 1,
                lines=tuple(lines[run_start : run_end + 1]),
            )
        )

    for i in range(len(lines) - 1):
        prev, cur = lines[i], lines[i + 1]
        broken = (
            _classify(prev) == "prose"
            and _classify(cur) == "prose"
            and _is_paragraph_hard_break(prev, cur)
        )
        if broken:
            if run_start is None:
                run_start = i
            continue
        if run_start is not None:
            close(i)
            run_start = None
    if run_start is not None:
        close(len(lines) - 1)
    return out


def _find_list_item_hard_breaks(lines: list[str]) -> list[Violation]:
    out: list[Violation] = []
    for i in range(len(lines) - 1):
        prev, cur = lines[i], lines[i + 1]
        # The bug: a list item immediately followed by a prose line at column
        # zero (no continuation indentation). An indented continuation is
        # valid markdown and renders as part of the item, so it is excluded.
        if _classify(prev) not in ("ulist", "olist"):
            continue
        if _classify(cur) != "prose":
            continue
        if cur[:1].isspace():
            continue
        # Flag the adjacency regardless of whether the item line ends a
        # sentence, unlike the paragraph case. Lazy continuation keeps the line
        # inside the bullet either way, so there is no shape here where the
        # break is intended — a deliberate second paragraph in a list item is
        # written with a blank line and an indent, which never reaches here.
        out.append(
            Violation(
                pattern="hard-newline-in-list-item",
                start=i + 1,
                end=i + 2,
                lines=(prev, cur),
            )
        )
    return out


def _row_cells(line: str) -> list[str]:
    """The cells of a pipe-delimited row, outer empties dropped.

    Splitting on cells rather than pattern-matching the raw line is what makes
    an alignment separator (`| -- | --: |`) distinguishable from a separator
    followed by data: the colon is part of the cell, not evidence of content.
    """
    parts = [p.strip() for p in _UNESCAPED_PIPE.split(line)]
    if parts and not parts[0]:
        parts = parts[1:]
    if parts and not parts[-1]:
        parts = parts[:-1]
    return parts


def _is_collapsed_table_row(line: str) -> bool:
    """One line carrying a separator row AND the row that should follow it.

    A well-formed table puts the separator on its own line, so every cell on
    that line is a separator cell and nothing follows. Collapsed, the line
    holds a run of separator cells with data cells after it, in one of two
    shapes:

        | A | B | --- | --- | a1 | b1 |   header + separator + data
        | --- | --- | a1 | b1 |           separator + data

    The run must therefore be preceded by either nothing or by exactly as many
    cells as the run is wide (its header). That width test is what keeps a data
    row using `--` as a placeholder — `| foo | -- | -- | done |` — out: its run
    of two is preceded by one cell, so it is not a header/separator pair.
    """
    cells = _row_cells(line)
    i = 0
    while i < len(cells):
        if not _SEP_CELL.match(cells[i]):
            i += 1
            continue
        start = i
        while i < len(cells) and _SEP_CELL.match(cells[i]):
            i += 1
        run = i - start
        if run < 2:
            continue
        if not any(cells[i:]):
            continue
        if start in (0, run):
            return True
    return False


def _find_collapsed_tables(lines: list[str]) -> list[Violation]:
    """Every table row that has swallowed the rows around it."""
    out: list[Violation] = []
    for i, line in enumerate(lines):
        if _classify(line) != "table":
            continue
        if _is_collapsed_table_row(line):
            out.append(
                Violation(
                    pattern="collapsed-table",
                    start=i + 1,
                    end=i + 1,
                    lines=(line,),
                )
            )
    return out


def find_violations(body: str) -> list[Violation]:
    """All three formatting bugs in the PR body, in line order."""
    if not body:
        return []
    lines = body.split("\n")
    masked = _strip_masked(lines)
    violations: list[Violation] = []
    violations.extend(_find_paragraph_hard_breaks(masked))
    violations.extend(_find_list_item_hard_breaks(masked))
    violations.extend(_find_collapsed_tables(masked))
    violations.sort(key=lambda v: (v.start, v.end))
    return violations


def _fetch_body(repo: str, pr: int) -> tuple[str, bool]:
    """The PR body and whether a bot wrote it, in one REST read.

    REST, not GraphQL: the repo's shared GraphQL budget is the scarce one, and
    `gh pr view --json` is GraphQL-backed. A non-zero exit raises rather than
    returning an empty body, so a failed read can never be mistaken for a PR
    with nothing wrong in it.
    """
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr}",
            "--jq",
            "{body: (.body // \"\"), bot: (.user.type == \"Bot\")}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api repos/{repo}/pulls/{pr} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh api repos/{repo}/pulls/{pr} returned unparsable JSON: {exc}"
        ) from exc
    return payload.get("body") or "", bool(payload.get("bot"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flag BAD PR-body formatting (hard breaks, collapsed tables) "
        "before a PR reaches review.",
    )
    parser.add_argument(
        "--repo",
        help="OWNER/REPO to fetch the PR body from via `gh api`",
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="PR number to fetch the body for (requires --repo)",
    )
    parser.add_argument(
        "--body-file",
        help="path to a file holding the PR body (use '-' for stdin)",
    )
    args = parser.parse_args(argv)

    if args.body_file and (args.repo or args.pr):
        parser.error("--body-file is mutually exclusive with --repo/--pr")
    if not args.body_file and not (args.repo and args.pr):
        parser.error("pass either --body-file or both --repo and --pr")

    try:
        if args.body_file:
            if args.body_file == "-":
                body = sys.stdin.read()
            else:
                with open(args.body_file, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            is_bot = False
        else:
            body, is_bot = _fetch_body(args.repo, args.pr)
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if is_bot:
        # A dependabot body is a several-hundred-line raw HTML changelog dump.
        # No human wrote it and no human will rewrite it.
        print("skipped: PR body was written by a bot")
        return 0

    violations = find_violations(body)
    if not violations:
        if not body.strip():
            print("no PR body to check (empty)")
        else:
            print("no PR-body formatting violations")
        return 0

    print(
        f"{len(violations)} PR-body formatting violation(s) found:"
    )
    for v in violations[:_MAX_REPORTED]:
        print()
        print(v.render())
    if len(violations) > _MAX_REPORTED:
        print(f"\n… and {len(violations) - _MAX_REPORTED} more, same patterns.")
    print(
        "\nFix by separating paragraphs with a blank line, continuing list items"
        " with indentation, or putting each table row on its own line."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())