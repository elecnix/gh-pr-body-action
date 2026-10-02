#!/usr/bin/env python3
"""The one place a PR body gets read from — file, stdin, or the REST API.

Three checkers in this action need the same four things before they can look at
a single line of the text: the body itself, whether a bot wrote it, a token, and
a way to tell a failed read from a clean body. Before this module each checker
answered those four questions for itself, in three languages, and they did not
agree:

  * the format checker built `https://api.github.com/repos/{repo}/pulls/{pr}`
    and resolved `GITHUB_TOKEN or GH_TOKEN`;
  * the prose checker built the same URL by hand and resolved `GITHUB_TOKEN`
    only, so a caller exporting `GH_TOKEN` alone got a format verdict and a
    prose check that refused to start;
  * the mermaid checker, in Node, reimplemented the same read over `fetch`.

That is the whole reason this module exists. The URL, the headers, the token
precedence, the bot-authority rule and the error taxonomy are decisions, and a
decision made three times is a decision that will be made a fourth time
differently. Here they are made once.

The invariants this module owns, and which the checkers rely on:

  * **Token precedence is `GITHUB_TOKEN` then `GH_TOKEN`** (`TOKEN_ENV_VARS`).
    `GITHUB_TOKEN` wins because that is what the composite action exports and
    what a workflow's own `github.token` lands in; `GH_TOKEN` is the fallback
    for a developer who already has one exported.
  * **REST, never GraphQL.** The repo's shared GraphQL budget is the scarce one
    and reading one pull request is not worth spending it on.
  * **urllib, never the GitHub CLI.** Running a check must need nothing but a
    token; shelling out would need a tool on `PATH` and would put
    attacker-influenceable PR text through a shell. A test in
    `test_check_pr_body_format.py` fails if either ever appears.
  * **Bot authorship is `user.type == "Bot"`.** A generated changelog dump is
    not a review surface a human wrote.
  * **A failed read raises `BodyReadError`; it never returns an empty body.**
    This is the load-bearing one. Every checker maps that exception to exit 2,
    so a body that could not be read can never be reported as clean — the same
    trap the prose checker already guards against on the other side of vale.

Standard library only, like the rest of this repo's Python. Nothing here
parses PR text, but a CI gate is exactly where a new dependency is least
welcome.

The Node sibling. `check-pr-body-mermaid.mjs` cannot import a Python module,
and reaching for a shell to do so would trade this module's whole point
for nothing. Its copy of the read therefore stays where it is — deleting it
would move the complexity, not concentrate it — but
`test_check_pr_body_mermaid.mjs` reads `TOKEN_ENV_VARS` out of this file and
asserts the Node side resolves the same names in the same order, so the one
divergence that has already bitten (prose reading `GITHUB_TOKEN` alone) cannot
come back unnoticed.

Usage:

    from pr_body_source import BodyReadError, fetch_pull_request, read_body_file

    try:
        body = read_body_file(path)          # or fetch_pull_request(repo, pr)
    except BodyReadError as exc:
        return 2                             # never 0: an unread body is not clean
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

# The API root, version-pinned by the `X-GitHub-Api-Version` header below.
API_ROOT = "https://api.github.com"

# Token precedence, in order. First non-empty wins. `check-pr-body-mermaid.mjs`
# resolves the same two names in the same order, and a test holds them to it.
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")

# A PR body can be large and the runner is a network hop away; 30s is the same
# budget the mermaid sibling has always used.
_TIMEOUT_SECONDS = 30

# The REST media type, the pinned API version, and a User-Agent that identifies
# this action in GitHub's logs rather than Python's default.
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "gh-pr-body-action",
}


class BodyReadError(RuntimeError):
    """The body could not be acquired.

    The one error type this module raises, and the reason the checkers catch
    exactly one exception: a taxonomy of failure modes is a taxonomy of
    mistakes, and every site that fails to enumerate them eventually reports a
    failed read as a clean body.
    """


@dataclass(frozen=True)
class Body:
    """One acquired body, and whether to skip it.

    `is_bot` travels with the text rather than being decided by the caller, so
    "a bot wrote this" cannot be answered differently by two checkers.
    """

    text: str
    is_bot: bool


def resolve_token(env: dict[str, str] | None = None) -> str | None:
    """The first token in `TOKEN_ENV_VARS` that is set and non-empty."""
    source = os.environ if env is None else env
    for name in TOKEN_ENV_VARS:
        if source.get(name):
            return source[name]
    return None


def read_body_file(path: str, stdin=None) -> str:
    """The body from a file, or from stdin when `path` is `-`.

    Decoded leniently (`errors="replace"`) and with no encoding games: a body
    that is not valid UTF-8 is still worth checking, and a replacement
    character cannot change a verdict about where a line breaks.
    """
    try:
        if path == "-":
            return (sys.stdin if stdin is None else stdin).read()
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, UnicodeError, ValueError) as exc:
        raise BodyReadError(f"could not read the body from {path}: {exc}") from exc


def fetch_pull_request(repo: str, pr: int, token: str | None = None) -> Body:
    """One pull request's body and author, in one REST read."""
    return _body_at(f"/repos/{repo}/pulls/{pr}", token)


def fetch_comment(repo: str, comment_id: int, token: str | None = None) -> Body:
    """One pull-request comment's body and author, in one REST read.

    `GET /repos/{owner}/{repo}/issues/comments/{id}` is the issues-comments
    endpoint, which serves pull-request comments too. A comment on an
    `issue_comment` event lives in the base repository, so the event's own token
    reads it and no extra scope work is needed.
    """
    return _body_at(f"/repos/{repo}/issues/comments/{comment_id}", token)


def _body_at(path: str, token: str | None) -> Body:
    payload = _rest_get(path, _require_token(token))
    return Body(
        text=payload.get("body") or "",
        is_bot=payload.get("user", {}).get("type") == "Bot",
    )


def _require_token(token: str | None) -> str:
    resolved = token or resolve_token()
    if not resolved:
        raise BodyReadError(
            "no token for the GitHub API call — set "
            + " or ".join(TOKEN_ENV_VARS)
        )
    return resolved


def _rest_get(path: str, token: str) -> dict:
    """GET one REST resource as JSON, or raise `BodyReadError`."""
    url = f"{API_ROOT}{path}"
    request = urllib.request.Request(
        url,
        headers={**_HEADERS, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Raised before URLError, which it subclasses: a 404 or a 403 has a
        # status worth printing and "no reason given" would throw it away.
        raise BodyReadError(f"GET {url} failed ({exc.code} {exc.reason})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BodyReadError(f"GET {url} failed: {exc}") from exc
    except ValueError as exc:
        # A 200 that is not JSON. Reading the status alone would call this a
        # clean body with no body in it.
        raise BodyReadError(f"GET {url} did not return JSON: {exc}") from exc