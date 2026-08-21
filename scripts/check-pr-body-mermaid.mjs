#!/usr/bin/env node
/* Flag a PR description whose mermaid diagram will not render, before review.

 * This docstring is the canonical write-up of the mechanic behind this half of
 * the gate. The repo README.md carries the usage entry
 * here. The Python sibling check-pr-body-format.py owns the line-classifier
 * shapes (hard breaks, collapsed tables); this checker owns one thing only: a
 * ```mermaid fenced block that mermaid itself rejects.
 *
 * Why this exists. GitHub renders a PR body's ```mermaid blocks with its own
 * pinned mermaid — currently the 11.x major. A diagram that mermaid's grammar
 * rejects renders as an error box, not as a diagram. The author does not see
 * an error box in the merged PR's body forever after: a dotted link carrying
 * BOTH text between the dots and a pipe label
 * (`A -. text .->|label| B`), which mermaid 11.x rejects with
 * `Expecting 'AMP', 'COLON', 'DOWN', ... got 'PIPE'`.
 *
 * Why parse with mermaid rather than guess. The defect is defined by mermaid's
 * grammar, and reimplementing that grammar in a lighter checker is how a gate
 * drifts from what GitHub actually renders. This checker runs the real
 * `mermaid.parse()` against the pinned mermaid and fails exactly when mermaid
 * fails. Two shapes that look like one bug are deliberately NOT flagged here —
 * they parse cleanly and render fine:
 *
 *     A -. text .-> B            # text between the dots, no label -> OK
 *     A -.->|text| B             # label only, no text between the dots -> OK
 *     A -. text .->|text2| B     # BOTH -> the incident-diagram shape -> FAIL
 *
 * All three probed against mermaid@11.16.1; the last is the only failure.
 *
 * The DOM shim. mermaid's parse path calls DOMPurify.sanitize, which needs a
 * window. In Node there is none, so this module installs a minimal jsdom window
 * (window/document/navigator and the element constructors) onto the global
 * before importing mermaid. That is the entire shim — mermaid 11.16.1 parse
 * needs nothing else (verified; `securityLevel: 'loose'` means parse never
 * exercises render). Installing the globals BEFORE the import is load-bearing:
 * a top-level await `import 'mermaid'` after assignment is what binds
 * DOMPurify's factory to the window.
 *
 * Why the extraction only trusts backtick `mermaid` fences. GitHub renders a
 * ```mermaid fenced block and a ~~~mermaid fenced block differently, and only
 * the backtick form is what an author uses for a diagram they expect to render
 * (GitHub does not render ~~~ fences as mermaid). So only ```mermaid blocks are
 * validated. A ``` line with any other language (or none) is skipped, as is any
 * ```mermaid that sits inside another fence. An unclosed ```mermaid fence is
 * reported — the *nothing-renders* case is exactly the class of silent defect
 * this gate is here to catch — but never crashes the checker: parse failures
 * and an unclosed fence both read as a finding, never as an exception.
 *
 * CLI, mirroring the Python sibling. Two input modes, same exit codes:
 *
 *     node scripts/check-pr-body-mermaid.mjs --body-file body.md  # or '-'
 *     node scripts/check-pr-body-mermaid.mjs --repo O/R --pr 1234 # via the REST API
 *
 *   0  no violations, empty body, or the author is a bot (a dependabot body is
 *      machine HTML nobody will fix).
 *   1  at least one unparsable mermaid block, each printed with its line range
 *      and the mermaid error.
 *   2  invalid usage, a failed API call, or an unreadable body — a failed
 *      read is never reported as clean.
 *
 * Dependency pinning. mermaid is pinned EXACTLY (no ^) in scripts/package.json
 * to the 11.16.1 this gate was calibrated against, matching the major GitHub
 * renders with. jsdom is a caret range: it only supplies the DOM shell and its
 * patch releases do not change mermaid's grammar; the load-bearing pin is the
 * mermaid one. CI installs via `npm ci` against the committed lockfile.
 */

import { JSDOM } from 'jsdom';

// ---------------------------------------------------------------- DOM shim
// Install a minimal jsdom window before mermaid is imported, so DOMPurify's
// factory (which mermaid's parse path calls) binds to a real window. Evaluated
// once, at module load, and shared by the CLI and the unit tests.
const _dom = new JSDOM('<!doctype html><html><body></body></html>');
const _win = _dom.window;
globalThis.window = _win;
globalThis.document = _win.document;
globalThis.HTMLElement = _win.HTMLElement;
globalThis.SVGElement = _win.SVGElement;
globalThis.Element = _win.Element;
globalThis.customElements = _win.customElements;
Object.defineProperty(globalThis, 'navigator', {
  value: _win.navigator,
  configurable: true,
});

const { default: mermaid } = await import('mermaid');
mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' });

// ------------------------------------------------------------------ helpers

// A ```` ``` ```` fence line, capturing the fence language ('' for a bare
// fence). Leading whitespace is allowed; a fence marker is a fence marker at any
// indent. An OPENING fence is 3+ backticks with an optional info string; GitHub
// accepts 3+ and a 4+-backtick ```mermaid block renders as a diagram just the
// same, so 3+ (not exactly 3) is validated. A closing fence is any 3+-backtick
// line while a fence is open, whatever its language — markdown closes a fence at
// the first unterminated fence line.
const _FENCE = /^\s*(`{3,})(\w*)\s*$/;
// The exact language that GitHub renders as a diagram. Other languages, and a
// bare fence, are skipped (a bare fence is where JIRA-style text blocks live).
const MERMAID_LANG = 'mermaid';

// Defang a body line the Actions runner would read as a workflow command, so
// echoing a finding can never execute `::error::` on a fork PR. Same guard as
// check-pr-body-format.py: indent is not enough, the runner strips it first.
function _defang(line) {
  if (line.trimStart().startsWith('::')) {
    return line.replace('::', "'::", 1);
  }
  return line;
}

/**
 * A single unparsable (or never-closed) mermaid block.
 * @typedef {{start: number, end: number, message: string}} BadBlock
 *   start/end are the 1-indexed inclusive line span of the whole ```mermaid
 *   fenced block — open fence line to closing fence line (or last body line
 *   when the fence never closes).
 */

/**
 * Every ```mermaid fenced block whose body mermaid.parse rejects, plus any
 * ```mermaid fence that never closes. Valid blocks, non-mermaid fences, and
 * fences inside other fences produce nothing. Runs mermaid.parse per block, so
 * a body with many diagrams costs one parse each.
 * @param {string} body
 * @returns {Promise<BadBlock[]>} findings in body order.
 */
export async function findBadMermaidBlocks(body) {
  if (!body) return [];
  const lines = body.split('\n');
  const out = [];

  let inFence = false; // inside ANY backtick fence
  let openLine = 0; // 0 = no mermaid block open
  let blockStart = 0; // 0-indexed fence-open line

  for (let i = 0; i < lines.length; i++) {
    const m = _FENCE.exec(lines[i]);
    if (!m) continue;

    const [, , lang] = m;
    if (inFence) {
      // Closes whatever block is open. A closer carries a language in markdown
      // only when it is the OPENING of a new fence; here it terminates the
      // current one, and any trailing language token is junk we ignore.
      if (openLine !== 0) {
        const content = lines.slice(blockStart + 1, i).join('\n');
        const end = i + 1; // this closer line, 1-indexed
        try {
          await mermaid.parse(content);
        } catch (err) {
          out.push({
            start: openLine, // fence-open line, 1-indexed
            end,
            message: _reason(err),
          });
        }
        openLine = 0;
      }
      inFence = false;
      continue;
    }

    // Not inside a fence: does this open a mermaid block?
    if (lang === MERMAID_LANG) {
      inFence = true;
      openLine = i + 1;
      blockStart = i;
    } else {
      // A bare or other-language fence opens a fence we skip entirely.
      inFence = true;
    }
  }

  // A ```mermaid fence that never closes: nothing renders. Report it rather
  // than dropping it on the floor — and certainly never let it throw.
  if (openLine !== 0) {
    out.push({
      start: openLine,
      end: lines.length,
      message: 'mermaid fence never closes (no closing ``` found)',
    });
  }

  out.sort((a, b) => a.start - b.start || a.end - b.end);
  return out;
}

// ------------------------------------------------------------------ fetching

/**
 * The PR body and whether a bot wrote it, in one REST read.
 *
 * A direct HTTPS call via fetch — no gh CLI required, so running the check
 * needs nothing but a token in the environment (GITHUB_TOKEN or GH_TOKEN).
 * REST, not GraphQL: the repo's shared GraphQL budget is the scarce one. A
 * failed read raises rather than returning an empty body, so a failed read can
 * never be mistaken for a PR with nothing wrong in it. Mirrors
 * check-pr-body-format.py.
 * @param {string} repo OWNER/REPO
 * @param {number} pr pull-request number
 * @param {string} token GitHub API token
 * @returns {Promise<{body: string, bot: boolean}>}
 */
export async function _fetchBody(repo, pr, token) {
  const url = `https://api.github.com/repos/${repo}/pulls/${pr}`;
  const response = await fetch(url, {
    headers: {
      Accept: 'application/vnd.github+json',
      Authorization: `Bearer ${token}`,
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'gh-pr-body-action',
    },
  });
  if (!response.ok) {
    throw new Error(`GET ${url} failed (${response.status} ${response.statusText})`);
  }
  const payload = await response.json();
  return { body: payload.body || '', bot: payload.user?.type === 'Bot' };
}

// ------------------------------------------------------------------------ CLI

function _usageError(message) {
  process.stderr.write(`error: ${message}\n`);
  return 2;
}

/**
 * The CLI entry point. Mirrors the Python sibling's exit codes (0 clean / 1
 * violation / 2 usage, fetch, or read failure) and its argument shape, so the
 * two halves of the gate compose in one workflow step.
 * @param {string[]} argv
 * @returns {Promise<number>}
 */
export async function main(argv) {
  let repo, pr, bodyFile;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    switch (a) {
      case '--repo': repo = next(); break;
      case '--pr': pr = Number(next()); break;
      case '--body-file': bodyFile = next(); break;
      default: return _usageError(`unknown argument: ${a}`);
    }
  }

  const hasNetwork = bodyFile === undefined && repo && pr;
  const hasFile = bodyFile !== undefined;
  if (hasNetwork && hasFile) {
    return _usageError('--body-file is mutually exclusive with --repo/--pr');
  }
  if (!hasNetwork && !hasFile) {
    return _usageError('pass either --body-file or both --repo and --pr');
  }
  if (hasFile && (repo || pr)) {
    return _usageError('--body-file is mutually exclusive with --repo/--pr');
  }

  let body, bot;
  try {
    if (hasFile) {
      if (bodyFile === '-') {
        body = await _readStdin();
      } else {
        const { readFileSync } = await import('node:fs');
        body = readFileSync(bodyFile, 'utf8');
      }
      bot = false;
    } else {
      const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN;
      if (!token) {
        process.stderr.write(
          'error: no token for the GitHub API call — set GITHUB_TOKEN (or GH_TOKEN)\n',
        );
        return 2;
      }
      ({ body, bot } = await _fetchBody(repo, pr, token));
    }
  } catch (err) {
    process.stderr.write(`error: ${err?.message || String(err)}\n`);
    return 2;
  }

  if (bot) {
    console.log('skipped: PR body was written by a bot');
    return 0;
  }

  const bad = await findBadMermaidBlocks(body);
  if (bad.length === 0) {
    if (!body.trim()) console.log('no PR body to check (empty)');
    else console.log('no unparsable mermaid diagrams in PR body');
    return 0;
  }

  console.log(`${bad.length} unparsable mermaid diagram(s) found:`);
  for (const b of bad) {
    console.log(`  lines ${b.start}–${b.end}: ${_defang(b.message)}`);
  }
  console.log(
    '\nFix by making the diagram parse: run it through the mermaid live editor,'
    + ' or drop text between a dotted link\'s dots and fold it into the label.'
  );
  return 1;
}

async function _readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}

// The one line of a mermaid parse error that tells the author what to do. The
// full message is a multi-line dump (``Parse error on line N:``, a source caret
// diagram, then the real reason ``Expecting ... got 'PIPE'``); the summary and
// the caret are noise in a CI finding, the Expecting/got line is the signal. A
// find that has no Expecting line (e.g. our own never-closes message) falls
// back to the first line.
function _reason(err) {
  const msg = String(err?.message || err);
  for (const raw of msg.split('\n')) {
    const line = raw.trim();
    if (line.startsWith('Expecting')) return line;
  }
  const nl = msg.indexOf('\n');
  return nl === -1 ? msg : msg.slice(0, nl);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exitCode = await main(process.argv.slice(2));
}
