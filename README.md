# gh-pr-body-action

A GitHub Action that flags a pull-request description that will render badly on GitHub — before it reaches review.

## Why

GitHub renders a PR body in **comment mode**, where a single newline becomes a `<br>`. A body hard-wrapped at 80 columns in an editor therefore arrives as a ragged column of broken lines, and the author never sees it because the editor shows the source. Verified against the live API:

```
POST /markdown  mode=gfm       "one line\nsecond line"
    -> "<p>one line<br>\nsecond line</p>"     # a PR body, a comment
POST /markdown  mode=markdown  "one line\nsecond line"
    -> "<p>one line\nsecond line</p>"         # a committed .md file
```

Same input, two surfaces, two results. Hard-wrapping is *correct* in a committed markdown file and a *defect* in a PR body.

## What it flags

| Pattern | The bug | The fix |
|---|---|---|
| `hard-newline-in-paragraph` | a prose line that stops mid-sentence and continues on the next line | let the line run long; the browser wraps it |
| `hard-newline-in-list-item` | a `- item` line followed by prose at column zero | indent the continuation by two spaces |
| `collapsed-table` | header, separator, and data rows pipe-joined onto one line | one row per line |
| `body-is-file-reference` | the whole body is a single `@/tmp/pr-body.md`-style file reference (the contents were never pasted) | paste the file's contents into the body |
| `unparsable-mermaid` | a ` ```mermaid ` block the mermaid parser rejects (renders as an error box) | fix the diagram; check it in the mermaid live editor |

The mermaid half runs the real `mermaid.parse()` against mermaid pinned to the 11.x major GitHub renders with, so a diagram fails exactly when GitHub shows an error box.

Bot-authored bodies are skipped. Code fences, HTML comments, nested lists, and bold pseudo-headings are not flagged — see the docstring of [`scripts/check-pr-body-format.py`](scripts/check-pr-body-format.py) for the full calibration and every deliberate suppression.

## Usage

```yaml
name: pr-body-format

on:
  pull_request:
    branches: [main]
    # `edited` re-runs the check once the author fixes the body — without it
    # the original red result stands.
    types: [opened, edited, reopened, synchronize]

permissions:
  contents: read
  pull-requests: read

jobs:
  pr-body-format:
    runs-on: ubuntu-latest
    steps:
      - uses: elecnix/gh-pr-body-action@v1
```

The body itself is read via the GitHub API, so the two rendering checks need no `actions/checkout`. The prose check does — it reads your rules out of the checkout. Add `actions/checkout` before the action to turn it on:

```yaml
    steps:
      - uses: actions/checkout@v5
      - uses: elecnix/gh-pr-body-action@v1
```

Without it the prose step reports "skipped, no rules found" and the job stays green.

### Inputs

| Input | Default | Description |
|---|---|---|
| `repo` | `github.repository` | `OWNER/REPO` of the pull request |
| `pr` | `github.event.pull_request.number` | pull-request number |
| `comment` | `github.event.comment.id` | PR-comment id. When set, the format checker runs against that comment instead of the PR description (the mermaid checker has no comment mode and is skipped). Mutually exclusive with `pr`. |
| `token` | `github.token` | token used to read the PR body (`pull-requests: read` suffices) |
| `node-version` | `22` | Node major for the mermaid checker |
| `prose` | `true` | lint the description against your repo's own prose rules. Set to `false` to turn the step off. |
| `prose-fail` | `false` | whether a prose finding fails the job. See [Prose](#prose-your-rules-not-ours). |
| `prose-rules-dir` | `github.workspace` | directory holding your `.vale.ini` |
| `vale-version` | `3.20.0` | vale release used by the prose step |

## Prose: your rules, not ours

The action also lints the description as prose — on by default. **It ships no rules.** It reads a `.vale.ini` out of your checkout and runs [vale](https://vale.sh) with it. A repo that has no `.vale.ini` is skipped, out loud, with the reason printed:

```
prose: skipped. No prose rules found at /home/runner/work/repo/repo/.vale.ini.
       This check carries no rules of its own; it reads the ones the
       repository ships. Add a .vale.ini (and run actions/checkout
       before this step) to turn it on.
```

That is the point. A prose standard belongs to the project that wrote it. A linter that shipped its own would be enforcing a stranger's taste on every caller, and a linter that stayed quiet about having no rules would hand you a green check that means nothing.

**It reports; it does not block.** `prose-fail` is `false` by default, so a finding annotates the run and the job stays green. Turn it on once your rules have earned it:

```yaml
      - uses: elecnix/gh-pr-body-action@v1
        with:
          prose-fail: 'true'    # a prose finding now fails the job
```

One case ignores that setting: if the linter could not run at all — vale missing, `vale sync` unable to fetch the packages your config pins — the job fails whatever `prose-fail` says. A check that did not start has not passed.

Run the same check by hand on a body before you send it:

```bash
python3 scripts/check-pr-body-prose.py --body-file body.md --rules-dir .
```

### Two ways this check could have lied

Both are the same shape — a run that finds nothing because it could never have found anything — and both are covered by a test.

**Vale picks its parser from the file extension.** A body written to a bare path, to a `.txt`, or piped on stdin lints as *plain text*: every markdown-scoped rule is skipped, nothing says so, and the run reports clean. The checker therefore always copies the body to a temporary `pr-body.md` before vale sees it, whatever the source was.

**Vale exits 0 on a warning.** Only an `error`-level alert makes it exit non-zero, so a rule pack written at `warning` — most of them — reports every finding and still returns success. The checker reads the verdict from vale's JSON output and uses the exit status only to tell "found alerts" apart from "could not run".

### Checking PR comments too

The same renderer rules apply to PR comments, and in comments the common failure is the paste accident. An agent posts the literal text `@/tmp/cite-reply.md` (the file reference) instead of the file's contents. In comment mode the `body-is-file-reference` rule also matches when the comment *opens* with one such token above real content; a path token inside a prose line, even the opening line, is ordinary content and is not reported.

Wire it as a second job beside the body check. The `edited` type matters even more than for bodies: comments get fixed by editing, so the check re-runs and goes back green.

```yaml
on:
  issue_comment:
    types: [created, edited]

jobs:
  pr-comment-format:
    # Keep repo issues out; this job is about PR comments only.
    if: github.event.issue.pull_request != null
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: elecnix/gh-pr-body-action@v1
        with:
          comment: ${{ github.event.comment.id }}
```

Comment text is attacker-influenceable: anyone with read access can write it. The checker defangs `::` workflow commands when it echoes findings, and the job above is read-only and never gains write scopes. On the same calibration posture as the body check, this is advisory rather than required. The comment rule measured zero false positives over recent human-authored PR comments, but the corpus is thin (only a few multi-paragraph comments and one table), so the corpus must grow before the rule could be more than advisory.

### Outputs

| Output | Description |
|---|---|
| `violations` | combined exit code: `0` clean, `1` violation(s), `2` usage or read failure |

Exit codes: `0` — no violations, empty body, or bot author. `1` — at least one violation, each printed with the pattern name, line range, and offending line(s). `2` — the body could not be read; a failed read is never reported as a clean body.

## Advisory, on purpose

On its calibration corpus (the 100 most recent PR bodies of the source repo) the check flags 11 of the 94 human-authored bodies — all already merged. Making it required would redden the trunk over descriptions nobody will rewrite. Wire it as **advisory** (checked but not required in your branch-protection ruleset): the check goes red without blocking the merge, and the author reads the finding and fixes or dismisses it.

## Running locally

Both checkers accept a saved body, offline, with no token:

```
python3 scripts/check-pr-body-format.py --body-file body.md   # or '-' for stdin
cd scripts && npm ci && node check-pr-body-mermaid.mjs --body-file ../body.md
```

Or read a live PR (no gh CLI needed — just a token):

```
GITHUB_TOKEN=ghp_... python3 scripts/check-pr-body-format.py --repo OWNER/REPO --pr 1234
cd scripts && node check-pr-body-mermaid.mjs --repo OWNER/REPO --pr 1234
```

Unit tests:

```
python3 scripts/test_check_pr_body_format.py
cd scripts && npm test
```

## Provenance

Extracted from the `pr-body-format` CI gate of a private repo, where it was calibrated against 100 real PR bodies and every suppression is pinned by a test.
