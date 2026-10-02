#!/usr/bin/env node
// -*- coding: utf-8 -*-
/* Unit tests for scripts/check-pr-body-mermaid.mjs.
 *
 * Every fixture is synthetic, and each bug shape is paired with the clean shape
 * it is easiest to confuse with it — same pairing rule as the Python sibling
 * test_check_pr_body_format.py. The load-bearing fixture is the merged diagram
 * that shipped an unparsable dotted-link shape and its fixed counterpart: the
 * gate exists because that shape slipped through, so a future
 * loosening of the parser re-breaks a test rather than a merge.
 *
 * These tests exercise the real mermaid.parse() against the pinned mermaid
 * (11.16.1, the major GitHub renders with) through the same jsdom shim the
 * checker uses, so a green run here means the grammar accepted it — the check
 * is verified by running mermaid, not by a reimplementation of mermaid's
 * grammar.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

const checker = await import('./check-pr-body-mermaid.mjs');
const findBadMermaidBlocks = checker.findBadMermaidBlocks;

// A diagram that mermaid rejects — line 6 carries the invalid shape
// `CB -. unclassified .->|"..."| SKIP[...]`: text between the dotted-link dots
// AND a pipe label. mermaid 11.16.1 rejects it; GitHub renders an error box.
// Every value here is invented; a fixture only has to be realistic markdown.
const DOTTED_LINK_BAD = `flowchart LR
    subgraph SW[upload service]
        PL[intake handler]
        CB[quota gate]
        PL -->|"over limit → 429 + RetryAfter"| CB
        CB -. unclassified .->|"no write, no report"| SKIP[request ends]
        PX[upstream returns 429] --> CB
        CB -->|"quota exhausted → open gate + report"| RPT[exhaustion notice → owner email]
    end
    subgraph CA[quota service]
        RS[Reserve read] --> SNAP[balance snapshot]
        SNAP --> L[levelFor: how many thresholds crossed]
        L -->|"crossing, not yet stamped"| ST[StampThresholdNotify]
        ST -->|"conditional, exactly one winner"| NS[notify seam]
        NS -->|"KindThreshold / KindExhausted"| MAIL[account owners: '$X remaining']
    end
    subgraph AD[console]
        B[GET /balance] --> BAL[$ left]
        T[GET/PUT /balance/thresholds] --> FIELDS[two USD fields]
    end
`;

// DOTTED_LINK_FIXED: the same diagram with the invalid link fixed — the
// `. unclassified .` text dropped so the label carries it. This is the clean
// pair for DOTTED_LINK_BAD.
const DOTTED_LINK_FIXED = DOTTED_LINK_BAD.replace(
  'CB -. unclassified .->|"no write, no report"| SKIP[request ends]',
  'CB -.->|"no write, no report"| SKIP[request ends]',
);

function wrapped(inner) {
  return '```mermaid\n' + inner + '\n```\n';
}

test('valid diagram inside a mermaid fence passes', async () => {
  const body = 'Some prose.\n\n```mermaid\nflowchart LR\nA -->|label| B\n```\n';
  assert.deepEqual(await findBadMermaidBlocks(body), []);
});

test('dotted link with text between the dots is valid', async () => {
  const body = wrapped('flowchart LR\nA -. text .-> B');
  assert.deepEqual(await findBadMermaidBlocks(body), []);
});

test('dotted link with only a pipe label is valid', async () => {
  const body = wrapped('flowchart LR\nA -.->|text| B');
  assert.deepEqual(await findBadMermaidBlocks(body), []);
});

test('the bad diagram fails; its fix passes (the load-bearing pair)', async () => {
  const badBody = wrapped(DOTTED_LINK_BAD);
  const bad = await findBadMermaidBlocks(badBody);
  assert.equal(bad.length, 1);
  // start is the ```mermaid fence-open line (line 1 by construction); end is
  // the matching closing ``` line (the last line of the wrapped body).
  assert.equal(bad[0].start, 1);
  assert.equal(bad[0].end, badBody.split('\n').length - 1);
  assert.ok(bad[0].message.length > 0, 'message should carry the parse error');

  const good = await findBadMermaidBlocks(wrapped(DOTTED_LINK_FIXED));
  assert.deepEqual(good, []);
});

test('the dotted-with-text-and-label shape fails on its own', async () => {
  const body = wrapped('flowchart LR\nA -. text .->|text2| B');
  const bad = await findBadMermaidBlocks(body);
  assert.equal(bad.length, 1);
  assert.match(bad[0].message, /Parse error|Expecting/i);
});

test('no mermaid fence yields no findings, even with prose', async () => {
  const body = 'Just prose, no fences at all.\n\n```\nplain code block\n```\n';
  assert.deepEqual(await findBadMermaidBlocks(body), []);
});

test('a non-mermaid fence is ignored', async () => {
  const body = '```\nflowchart LR\nA -. text .->|text2| B\n```\n';
  assert.deepEqual(await findBadMermaidBlocks(body), []);
});

test('a mermaid diagram in a code-fence-with-language that is not mermaid is ignored', async () => {
  const body = '```text\nflowchart LR\nA --> B\n```\n';
  assert.deepEqual(await findBadMermaidBlocks(body), []);
});

test('an unclosed mermaid fence is reported, not crashed on', async () => {
  const body = '```mermaid\nflowchart LR\nA -. text .->|text2| B\n';
  const bad = await findBadMermaidBlocks(body);
  assert.equal(bad.length, 1);
  assert.match(bad[0].message, /fence never closes/i);
});

test('multiple bad diagrams each get their own finding with line ranges', async () => {
  // Exact lines, so the ranges are asserted against the fence positions:
  //   L1  Intro prose.
  //   L2  (blank)
  //   L3  ```mermaid          <- block 1 opens
  //   L4  flowchart LR
  //   L5  A -. t .->|x| B
  //   L6  ```                 <- block 1 closes
  //   L7  (blank)
  //   L8  Middle prose.
  //   L9  (blank)
  //   L10 ```mermaid          <- block 2 opens
  //   L11 flowchart LR
  //   L12 A --> B
  //   L13 B -. t .->|y| C
  //   L14 ```                 <- block 2 closes
  const body = [
    'Intro prose.',
    '',
    '```mermaid',
    'flowchart LR',
    'A -. t .->|x| B',
    '```',
    '',
    'Middle prose.',
    '',
    '```mermaid',
    'flowchart LR',
    'A --> B',
    'B -. t .->|y| C',
    '```',
  ].join('\n');
  const bad = await findBadMermaidBlocks(body);
  assert.equal(bad.length, 2);
  assert.equal(bad[0].start, 3);
  assert.equal(bad[0].end, 6);
  assert.equal(bad[1].start, 10);
  assert.equal(bad[1].end, 14);
});

test('a tildes fence is not treated as a mermaid fence', async () => {
  const body = '~~~mermaid\nflowchart LR\nA -. text .->|x| B\n~~~\n';
  assert.deepEqual(await findBadMermaidBlocks(body), []);
});

test('a 4+-backtick mermaid fence is validated too', async () => {
  // GitHub accepts fences of 3+ backticks; a 4+-backtick ```mermaid block must
  // not be skipped just because its opener has more than three backticks.
  const bad = '````mermaid\nflowchart LR\nA -. text .->|x| B\n````\n';
  const good = '````mermaid\nflowchart LR\nA --> B\n````\n';
  assert.equal((await findBadMermaidBlocks(bad)).length, 1);
  assert.deepEqual(await findBadMermaidBlocks(good), []);
});

test('empty body yields no findings', async () => {
  assert.deepEqual(await findBadMermaidBlocks(''), []);
  assert.deepEqual(await findBadMermaidBlocks('   \n\n  \n'), []);
});

// ------------------------------------------------- REST fetch (no gh CLI)

test('_fetchBody hits the REST API with a bearer token, no gh CLI', async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => ({ body: 'a clean body', user: { type: 'User' } }),
    };
  };
  try {
    const { body, bot } = await checker._fetchBody('o/r', 7, 'tok-123');
    assert.equal(body, 'a clean body');
    assert.equal(bot, false);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'https://api.github.com/repos/o/r/pulls/7');
    assert.equal(calls[0].init.headers.Authorization, 'Bearer tok-123');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('_fetchBody detects a bot author', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ body: '<html>changelog</html>', user: { type: 'Bot' } }),
  });
  try {
    const { body, bot } = await checker._fetchBody('o/r', 7, 'tok');
    assert.equal(bot, true);
    assert.ok(body.includes('changelog'));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('_fetchBody raises on a failed read, never returns a clean body', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false,
    status: 404,
    statusText: 'Not Found',
    json: async () => ({}),
  });
  try {
    await assert.rejects(() => checker._fetchBody('o/r', 7, 'tok'), /404/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ------------------------------------------- parity with the Python seam

/*
 * The acquisition contract is owned by scripts/pr_body_source.py, which this
 * file cannot import. Rather than let the copy rot, these tests read the
 * Python module's source and hold this one to it. The token precedence is the
 * part that has already drifted once — the prose checker resolved
 * `GITHUB_TOKEN` alone while this file and the format checker accepted
 * `GH_TOKEN` as well, so a caller exporting one of the two got a verdict from
 * two checkers and a refusal from the third.
 */

const readFileSync = (await import('node:fs')).readFileSync;
const { fileURLToPath } = await import('node:url');
const seamPath = fileURLToPath(new URL('./pr_body_source.py', import.meta.url));

/** The names in the Python seam's `TOKEN_ENV_VARS`, in order. */
function seamTokenEnvVars() {
  const source = readFileSync(seamPath, 'utf8');
  const match = source.match(/^TOKEN_ENV_VARS\s*=\s*\(([^)]*)\)/m);
  assert.ok(match, 'TOKEN_ENV_VARS is missing from scripts/pr_body_source.py');
  return match[1]
    .split(',')
    .map((name) => name.trim().replace(/^["']|["']$/g, ''))
    .filter(Boolean);
}

test('the token precedence matches the Python seam, name for name and in order', () => {
  // The assertion is written as equality with the seam, not as a literal, so
  // changing the seam's precedence is a decision that updates both sides at
  // once in review rather than a silent one-sided edit.
  assert.deepEqual([...checker._TOKEN_ENV_VARS], seamTokenEnvVars());
});

test('_resolveToken prefers the first name and falls back to the second', () => {
  const [first, second] = checker._TOKEN_ENV_VARS;
  assert.equal(checker._resolveToken({ [first]: 'a', [second]: 'b' }), 'a');
  assert.equal(checker._resolveToken({ [second]: 'b' }), 'b');
  assert.equal(checker._resolveToken({ [first]: '', [second]: 'b' }), 'b');
  assert.equal(checker._resolveToken({}), null);
});
