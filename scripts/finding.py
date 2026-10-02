#!/usr/bin/env python3
"""The one finding shape, and the one defang every line of it passes through.

This module is the canonical write-up of both. `check-pr-body-format.py` and
`check-pr-body-prose.py` import from it; `finding.mjs` is its Node twin and
carries the same two decisions for the mermaid checker, so the two languages
never drift apart on what a finding is or how a line is made safe to print.

Why one shape. Three checkers used to invent their own. The format checker had
a `Violation` dataclass carrying a pattern, a line span and the offending lines;
the mermaid checker had a `BadBlock` object carrying a line span and a parser
message and no pattern at all; the prose checker had bare strings with neither,
because vale reports line numbers inside a formatted string nobody parsed. Three
answers to "what did this run find", three places to get the reporting wrong,
and a README table that listed one identifier the mermaid checker never emitted.

A finding is now four things, and always all four:

    pattern    a stable identifier, so a reader (and a search) can name the bug
    start,end  the 1-indexed inclusive line span, in the body being checked
    message    the checker's own words, when it has any (the parser's reason)
    lines      the author's own evidence lines, when it has any

`render()` is the only way a finding reaches stdout, which is what makes the
guarantee below structural rather than incidental.

The guarantee
-------------
A PR body is attacker-influenceable on a fork PR, and the Actions runner parses
any stdout line whose first non-space characters are `::`. So a body line of
`::error::` or `::stop-commands::<tok>` would be *executed* rather than printed
as evidence. Every line of every finding goes through `defang()` on its way out.

The one defang mechanism is a visible quote: the leading `::` becomes `'::`.
That spelling was chosen over the zero-width space the prose checker used, and
the argument is worth keeping because the alternative is not obviously wrong:

  * **It is greppable.** In a CI log you can enumerate every neutralised
    command with `grep "'::"`. A zero-width space has no printable form, so the
    only way to find one is to copy the bytes out of the log. When the thing
    being defended against is a line the runner may execute, "can a human
    account for every instance afterwards" is part of the guarantee, not a
    nicety.
  * **It is visible in the source.** The zero-width replacement was a literal
    U+200B sitting inside a raw string, so nobody reading the diff could see it
    — and any editor, formatter or well-meaning "strip stray unicode" pass would
    remove it and silently disable the guard. A quote announces itself.
  * **It is visible in the output.** The reader can see that the first two
    characters were rewritten and compare the rest verbatim. A zero-width space
    makes mutated evidence look untouched, which is the one behaviour a security
    control must not have.
  * **It keeps CI logs ASCII.** Logs get copied, grepped, diffed and archived.

The honest cost is that the echoed line is not byte-identical to the body's
line. That is the point: fidelity to attacker-controlled bytes is not a property
this output has, and the line is already rendered as evidence under a synthetic
head, so only the first two characters of a line that would otherwise have run
are touched.

Indenting is not a substitute and never was: the runner strips leading
whitespace before it looks for `::`. That is why `defang` rewrites rather than
shifts, and why `render()` can indent for the reader's sake without having to
reason about the runner's at all.

Two layers, and which one carries depends on the shape. A finding that echoes
the author's own text — the format checker, and any evidence lines a future
checker adds — has nothing in front of it, so the defang is the guarantee. A
finding whose message carries the checker's own prefix in front of it, which is
every message this action renders, is already safe because that prefix is not a
colon; the defang sits underneath it. Both live in `render()`, which is the
only path a finding has to stdout, so neither is anyone's private job.

The caps
--------
`MAX_ECHOED_LINES` and `MAX_REPORTED` live here too, because "how much of a
finding to print" is part of the shape and was previously one checker's private
constant. A hard-wrapped body is one defect with one fix, so echoing all 300 of
its lines back buys the author nothing and buries every other finding in the run
log; past the first handful of findings the reader has the message. Both caps
are summarised by count rather than silently applied.

No third-party imports: this module is loaded by scripts that run on the
standard library only, in a CI gate that parses attacker-influenceable text.
"""

from __future__ import annotations

from dataclasses import dataclass

# How many evidence lines one finding echoes before summarising the rest.
MAX_ECHOED_LINES = 4

# How many findings one run prints in full before summarising the rest.
MAX_REPORTED = 15

# Every rendered evidence line carries this indent. It is for the reader; the
# runner strips it, so it is not part of the defang.
_INDENT = "    "

# What a leading `::` is rewritten to. A quote, deliberately — see the module
# docstring for why this and not a zero-width space.
_QUOTE = "'::"

_WORKFLOW_COMMAND = "::"


def defang(line: str) -> str:
    """Neutralise a line the Actions runner would read as a workflow command.

    Only a *leading* `::` is rewritten, and only the first one. A `::` in the
    middle of a line (`see foo::bar`) can never be a command — the runner only
    looks at the start of the line — so rewriting one would corrupt evidence for
    no gain. Returns the line unchanged when there is nothing to do.
    """
    if line.lstrip().startswith(_WORKFLOW_COMMAND):
        return line.replace(_WORKFLOW_COMMAND, _QUOTE, 1)
    return line


def _indented(block: str) -> list[str]:
    """Every line of `block`, defanged and indented, blanks dropped.

    A finding's message can be more than one line (a parser reason, a vale
    message with an embedded newline), so the whole block is defanged line by
    line rather than as one string — otherwise a `::` that only opens a
    continuation line would survive untouched.
    """
    out = []
    for raw in block.split("\n"):
        if raw.strip():
            out.append(_INDENT + defang(raw))
    return out


@dataclass(frozen=True)
class Finding:
    """One defect, as this action reports it. See the module docstring.

    `start` and `end` are 1-indexed and inclusive, in the body being checked.
    `message` is the checker's own words; `lines` is the author's evidence. A
    finding may carry either, both or neither — the mermaid checker has a
    message and no evidence, the prose checker the reverse — and `render()`
    reads the same either way.
    """

    pattern: str
    start: int
    end: int
    message: str = ""
    lines: tuple[str, ...] = ()

    @property
    def span(self) -> str:
        """The line range as a reader sees it: `line 7`, or `lines 7–11`."""
        if self.end == self.start:
            return f"line {self.start}"
        return f"lines {self.start}–{self.end}"

    def render(self) -> str:
        """The finding as it is printed, defanged, with the echo cap applied."""
        out = [f"{self.pattern} ({self.span})"]
        if self.message:
            out.extend(_indented(self.message))
        shown = self.lines[:MAX_ECHOED_LINES]
        for raw in shown:
            out.append(_INDENT + defang(raw))
        hidden = len(self.lines) - len(shown)
        if hidden > 0:
            out.append(f"{_INDENT}… {hidden} more line(s) in the same run")
        return "\n".join(out)


def render_report(
    findings: list[Finding], header: str, footers: list[str] | None = None
) -> str:
    """A whole run's findings, under one header, above any footers.

    The report cap lives here rather than in each checker so the "… and N more"
    summary reads the same everywhere, and so there is one place to test it.
    """
    parts = [header]
    for finding in findings[:MAX_REPORTED]:
        parts.append("")
        parts.append(finding.render())
    if len(findings) > MAX_REPORTED:
        parts.append("")
        parts.append(f"… and {len(findings) - MAX_REPORTED} more, same patterns.")
    parts.extend(footers or [])
    return "\n".join(parts)
