// The one finding shape, and the one defang every line of it passes through.
//
// This is the Node twin of scripts/finding.py, and it carries the same two
// decisions for the mermaid checker: what a finding is, and how a line of one
// is made safe to print. The two modules cannot share code — one is Python
// loaded by a stdlib-only script, the other is ESM loaded by the mermaid
// checker — so they are kept in step by name, by shape and by the tests that
// pin the rendered output on both sides. Read finding.py's docstring for the
// reasoning; it is the canonical write-up.
//
// A finding is four things, and always all four:
//
//   pattern    a stable identifier, so a reader (and a search) can name the bug
//   start,end  the 1-indexed inclusive line span, in the body being checked
//   message    the checker's own words, when it has any
//   lines      the author's own evidence lines, when it has any
//
// The mermaid checker fills in the first three and leaves `lines` empty: the
// parser's own reason ("Expecting … got 'PIPE'") already names the offending
// token, so echoing the diagram source would put more attacker-controlled
// bytes into the log for no diagnostic gain. The format checker fills in the
// first, second and fourth. `render()` is the only way a finding reaches
// stdout, which is what makes the defang structural rather than incidental.

/** Evidence lines one finding echoes before summarising the rest. */
export const MAX_ECHOED_LINES = 4;

/** Findings one run prints in full before summarising the rest. */
export const MAX_REPORTED = 15;

// Every rendered line carries this indent. It is for the reader; the Actions
// runner strips leading whitespace before it looks for a workflow command, so
// it is not part of the defang and must never be relied on as one.
const INDENT = '    ';

const WORKFLOW_COMMAND = '::';
// A quote, not a zero-width space: greppable in a CI log, visible in the diff,
// and visible in the output, so a reader can see what was rewritten. See
// scripts/finding.py for the full argument.
const QUOTE = "'::";

/**
 * Neutralise a line the Actions runner would read as a workflow command.
 *
 * Only a *leading* `::` is rewritten, and only the first one: a `::` mid-line
 * can never be a command, and rewriting it would corrupt evidence for nothing.
 * @param {string} line
 * @returns {string}
 */
export function defang(line) {
  if (line.trimStart().startsWith(WORKFLOW_COMMAND)) {
    return line.replace(WORKFLOW_COMMAND, QUOTE);
  }
  return line;
}

/** Every line of `block`, defanged and indented, blanks dropped. */
function _indented(block) {
  const out = [];
  for (const raw of block.split('\n')) {
    if (raw.trim()) out.push(INDENT + defang(raw));
  }
  return out;
}

/**
 * One defect, as this action reports it.
 *
 * A finding may carry a message, evidence lines, both or neither; `render()`
 * reads the same either way, so a checker only fills in what it actually knows.
 */
export class Finding {
  /**
   * @param {string} pattern stable identifier for the bug
   * @param {number} start 1-indexed inclusive first line of the span
   * @param {number} [end] 1-indexed inclusive last line; defaults to `start`
   * @param {{message?: string, lines?: string[]}} [rest]
   */
  constructor(pattern, start, end, { message = '', lines = [] } = {}) {
    this.pattern = pattern;
    this.start = start;
    this.end = end ?? start;
    this.message = message;
    this.lines = [...lines];
  }

  /** The line range as a reader sees it: `line 7`, or `lines 7–11`. */
  get span() {
    return this.start === this.end
      ? `line ${this.start}`
      : `lines ${this.start}–${this.end}`;
  }

  /** The finding as it is printed, defanged, with the echo cap applied. */
  render() {
    const out = [`${this.pattern} (${this.span})`];
    if (this.message) out.push(..._indented(this.message));
    const shown = this.lines.slice(0, MAX_ECHOED_LINES);
    for (const raw of shown) out.push(INDENT + defang(raw));
    const hidden = this.lines.length - shown.length;
    if (hidden > 0) out.push(`${INDENT}… ${hidden} more line(s) in the same run`);
    return out.join('\n');
  }
}

/**
 * A whole run's findings, under one header, above any footers. The report cap
 * lives here so the "… and N more" summary reads the same in every checker.
 * @param {Finding[]} findings
 * @param {string} header
 * @param {string[]} [footers]
 * @returns {string}
 */
export function renderReport(findings, header, footers = []) {
  const parts = [header];
  for (const finding of findings.slice(0, MAX_REPORTED)) {
    parts.push('');
    parts.push(finding.render());
  }
  if (findings.length > MAX_REPORTED) {
    parts.push('');
    parts.push(`… and ${findings.length - MAX_REPORTED} more, same patterns.`);
  }
  parts.push(...footers);
  return parts.join('\n');
}
