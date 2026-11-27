"""The re-check of a placeholder link, by a plain request to its host's API, not by the agent: a
GitHub repository (`api.github.com/repos/<owner>/<repo>`, its contents, tree and releases) or a
Hugging Face model, dataset or space (`huggingface.co/api/<kind>s/<owner>/<name>`), at the
repository's own page; a link anywhere else is not checked again (`checkable`). It is the same
check as the finder's in deep (`deep/src/hostcheck.ts`), one table of fixture responses
(`deep/tests/fixtures/host-checks.json`) driving both suites:

- Hugging Face: its `gated` flag (`true`, "auto", "manual"; `false` or absent is not; anything
  else says nothing) is gated; else, for weights, a listed weight file is public;
  for code, a listed file beside the filler; a repository with neither is a placeholder. A 401
  or 404 is missing (it answers 401 for a repository it does not have).
- GitHub: for code, a file beside the filler in its contents is public; for weights, a weight
  file in its tree or a release asset with a weight file's name; anything else a placeholder. A
  404 is missing.

A redirect (a renamed repository: GitHub answers 301, Hugging Face 307) is followed, `REDIRECTS`
at most, each hop over HTTPS on the host asked, as deep's check follows it; a hop elsewhere is
not, and says nothing. An answer that is not the API's (not JSON, past `MAX_BYTES`, not the
shape the API gives) says nothing either: the re-check counts it, and checks again later.

An Adapter over today's HTTP client until the Fetch gate (A5)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from litwatch.core.links import Kind, State

log = logging.getLogger(__name__)

TIMEOUT = 30.0
GITHUB_API = "api.github.com"
# What a repository holds beside its code when it holds no code yet: a README, a licence, its
# git and GitHub files, a citation.
_FILLER = re.compile(
    r"^(readme|licen[cs]e|copying|citation|notice)(\.\w+)?$|^\.git(ignore|attributes|hub)?$",
    re.IGNORECASE,
)
# A file of weights, whole or one shard of them (`model-00001-of-00004.safetensors`,
# `model.gguf.part1of2`).
_WEIGHTS = re.compile(
    r"\.(safetensors|bin|pt|pth|ckpt|gguf|onnx|h5|msgpack|npz)(\.part\d+(of\d+)?)?$",
    re.IGNORECASE,
)
# How many redirects a re-check follows, and the most an answer may hold: a large GitHub tree
# fits. Deep's check takes the same (`deep/src/hostcheck.ts`).
REDIRECTS = 3
MAX_BYTES = 8_000_000

type Checked = tuple[State, str | None]  # its state, and its license when the host gives one


def _target(url: str) -> tuple[str, str] | None:
    """The API page that says what the link holds, and its host's name; None for a link on a
    host not checked, or not a repository's own page (a file or folder in one)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if parts.query:
        return None
    if host == "github.com" and len(path) == 2:
        return f"https://{GITHUB_API}/repos/{path[0]}/{path[1].removesuffix('.git')}", "github"
    if host == "huggingface.co" and path:
        kind, rest = (path[0], path[1:]) if path[0] in ("datasets", "spaces") else ("models", path)
        if len(rest) == 2:
            return f"https://huggingface.co/api/{kind}/{rest[0]}/{rest[1]}", "huggingface"
    return None


def checkable(url: str) -> bool:
    """Whether a re-check can say what the link holds: a repository on GitHub or Hugging Face."""
    return _target(url) is not None


def _filler(names: list[str]) -> bool:
    return all(_FILLER.match(n.rsplit("/", 1)[-1]) for n in names)


def _strings(items: Any, field: str) -> list[str]:
    if not isinstance(items, list):
        return []
    found = [i.get(field) for i in items if isinstance(i, dict)]
    return [s for s in found if isinstance(s, str)]


def _github_license(repo: dict[str, Any]) -> str | None:
    found = repo.get("license")
    if not isinstance(found, dict):
        return None
    spdx = found.get("spdx_id")
    name = found.get("name")
    if isinstance(spdx, str) and spdx and spdx != "NOASSERTION":
        return spdx
    return name if isinstance(name, str) else None


def _hf_license(model: dict[str, Any]) -> str | None:
    card, tags = model.get("cardData"), model.get("tags")
    if isinstance(card, dict) and isinstance(card.get("license"), str):
        return card["license"]
    if not isinstance(tags, list):
        return None
    tags = [t for t in tags if isinstance(t, str) and t.startswith("license:")]
    return tags[0].removeprefix("license:") if tags else None


def client() -> httpx.AsyncClient:
    """The client a re-check asks its host with (tests stand a fake host in for it)."""
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"user-agent": "litwatch"})


async def check(url: str, kind: Kind) -> Checked | None:
    """What the `kind` link holds now, as the module says; None when the host could not say
    (it is not checked here, did not answer, refused, was rate limited, or answered what is not
    its API's)."""
    target = _target(url)
    if target is None:
        return None
    api, host = target
    try:
        async with client() as http:
            if host == "github":
                return await _github(http, api, kind)
            return await _huggingface(http, api, kind)
    except httpx.HTTPError as exc:
        log.warning("re-checking %s: %s could not say: %s", url, host, exc)
        return None


@dataclass(frozen=True)
class _Answer:
    """What a host's API answered: its status, and its JSON when it is a 2xx (else None)."""

    status: int
    body: Any = None


async def _get(http: httpx.AsyncClient, url: str) -> _Answer | None:
    """GET `url` of a host's API, as deep's check asks it (`apiGet`): each redirect followed,
    `REDIRECTS` at most, only over HTTPS on the host asked, the answer to a hop elsewhere
    standing (it says nothing); a 2xx's JSON read, `MAX_BYTES` at most. None for a 2xx that is
    not JSON or holds more, or for redirects past `REDIRECTS`."""
    at, host = httpx.URL(url), httpx.URL(url).host
    for _ in range(REDIRECTS + 1):
        async with http.stream("GET", at) as resp:
            if resp.is_redirect and "location" in resp.headers:
                moved = at.join(resp.headers["location"])
                if moved.scheme != "https" or moved.host != host:
                    return _Answer(resp.status_code)
                at = moved
                continue
            if not 200 <= resp.status_code < 300:
                return _Answer(resp.status_code)
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return None
        try:
            return _Answer(resp.status_code, json.loads(body))
        except ValueError:
            return None
    return None


async def _github(http: httpx.AsyncClient, api: str, kind: Kind) -> Checked | None:
    repo = await _get(http, api)
    if repo is None or repo.status not in (200, 404):
        return None
    if repo.status == 404:
        return "missing", None
    found = repo.body
    if not isinstance(found, dict) or not isinstance(found.get("full_name"), str):
        return None
    license = _github_license(found)
    named = f"https://{GITHUB_API}/repos/{found['full_name']}"  # as GitHub now names it
    if kind == "code":
        contents = await _get(http, f"{named}/contents")
        if contents is not None and contents.status == 404:  # no commit, no contents
            return "placeholder", license
        if contents is None or contents.status != 200 or not isinstance(contents.body, list):
            return None
        return ("placeholder" if _filler(_strings(contents.body, "path")) else "public"), license
    branch = found.get("default_branch") if isinstance(found.get("default_branch"), str) else "HEAD"
    tree = await _get(http, f"{named}/git/trees/{quote(branch, safe='')}?recursive=1")
    if tree is None or tree.status not in (200, 404, 409):  # 404, 409: no commit yet
        return None
    if tree.status == 200:
        if not isinstance(tree.body, dict) or not isinstance(tree.body.get("tree"), list):
            return None
        if any(_WEIGHTS.search(p) for p in _strings(tree.body["tree"], "path")):
            return "public", license
    releases = await _get(http, f"{named}/releases?per_page=100")
    if releases is None or releases.status != 200 or not isinstance(releases.body, list):
        return None
    assets = [
        a for r in releases.body if isinstance(r, dict) for a in _strings(r.get("assets"), "name")
    ]
    return ("public" if any(_WEIGHTS.search(a) for a in assets) else "placeholder"), license


async def _huggingface(http: httpx.AsyncClient, api: str, kind: Kind) -> Checked | None:
    resp = await _get(http, api)
    if resp is None or resp.status not in (200, 401, 404):
        return None
    if resp.status in (401, 404):  # Hugging Face answers 401 for a repository it has not
        return "missing", None
    found = resp.body
    if not isinstance(found, dict):
        return None
    license = _hf_license(found)
    gated = found.get("gated", False)  # absent: not gated
    if not (gated is False or gated is True or gated in ("auto", "manual")):
        return None  # not a value its API gives
    if gated is not False:
        return "gated", license
    if not isinstance(found.get("siblings"), list):
        return None
    names = _strings(found["siblings"], "rfilename")
    held = any(_WEIGHTS.search(n) for n in names) if kind == "weights" else not _filler(names)
    return ("public" if held else "placeholder"), license
