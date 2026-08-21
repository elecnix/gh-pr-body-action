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

No `actions/checkout` is needed — the action reads the body via the GitHub API.

### Inputs

| Input | Default | Description |
|---|---|---|
| `repo` | `github.repository` | `OWNER/REPO` of the pull request |
| `pr` | `github.event.pull_request.number` | pull-request number |
| `token` | `github.token` | token used to read the PR body (`pull-requests: read` suffices) |
| `node-version` | `22` | Node major for the mermaid checker |

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

Or read a live PR:

```
GH_TOKEN=ghp_... python3 scripts/check-pr-body-format.py --repo OWNER/REPO --pr 1234
```

Unit tests:

```
python3 scripts/test_check_pr_body_format.py
cd scripts && npm test
```

## Provenance

Extracted from the `pr-body-format` CI gate of a private repo, where it was calibrated against 100 real PR bodies and every suppression is pinned by a test.

<!-- smoke test placeholder -->
