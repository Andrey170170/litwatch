"""The re-check of a placeholder link, by a plain request to its host's API, not by the agent: a
GitHub repository (`api.github.com/repos/<owner>/<repo>`, its contents, tree and releases) or a
Hugging Face model, dataset or space (`huggingface.co/api/<kind>s/<owner>/<name>`), at the
repository's own page; a link anywhere else is not checked again (`checkable`). It is the same
check as the finder's in deep (`deep/src/hostcheck.ts`), one table of fixture responses
(`deep/tests/fixtures/host-checks.json`) driving both suites:

- Hugging Face: its `gated` flag is gated; else, for weights, a listed weight file is public;
  for code, a listed file beside the filler; a repository with neither is a placeholder. A 401
  or 404 is missing (it answers 401 for a repository it does not have).
- GitHub: for code, a file beside the filler in its contents is public; for weights, a weight
  file in its tree or a release asset with a weight file's name; anything else a placeholder. A
  404 is missing. Its redirects (a renamed repository) are followed within its API.

An Adapter over today's HTTP client until the Fetch gate (A5)."""

from __future__ import annotations

import logging
import re
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
# How many of GitHub's redirects a re-check follows.
REDIRECTS = 3

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
    found = repo.get("license") or {}
    spdx = found.get("spdx_id")
    name = found.get("name")
    if isinstance(spdx, str) and spdx and spdx != "NOASSERTION":
        return spdx
    return name if isinstance(name, str) else None


def _hf_license(model: dict[str, Any]) -> str | None:
    card = model.get("cardData") or {}
    if isinstance(card.get("license"), str):
        return card["license"]
    tags = [t for t in model.get("tags") or [] if isinstance(t, str) and t.startswith("license:")]
    return tags[0].removeprefix("license:") if tags else None


def client() -> httpx.AsyncClient:
    """The client a re-check asks its host with (tests stand a fake host in for it)."""
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"user-agent": "litwatch"})


async def check(url: str, kind: Kind) -> Checked | None:
    """What the `kind` link holds now, as the module says; None when the host could not say
    (it is not checked here, did not answer, refused, or was rate limited)."""
    target = _target(url)
    if target is None:
        return None
    api, host = target
    try:
        async with client() as http:
            if host == "github":
                return await _github(http, api, kind)
            return await _huggingface(http, api, kind)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("re-checking %s: %s could not say: %s", url, host, exc)
        return None


async def _github_get(http: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET `url`, following GitHub's redirects within its API, `REDIRECTS` at most."""
    resp = await http.get(url)
    for _ in range(REDIRECTS):
        if not resp.is_redirect or "location" not in resp.headers:
            break
        moved = resp.url.join(resp.headers["location"])
        if moved.scheme != "https" or moved.host != GITHUB_API:
            break
        resp = await http.get(moved)
    return resp


async def _github(http: httpx.AsyncClient, api: str, kind: Kind) -> Checked | None:
    repo = await _github_get(http, api)
    if repo.status_code == 404:
        return "missing", None
    if repo.status_code != 200:
        return None
    found = repo.json()
    license = _github_license(found)
    named = f"https://{GITHUB_API}/repos/{found['full_name']}"  # as GitHub now names it
    if kind == "code":
        contents = await _github_get(http, f"{named}/contents")
        if contents.status_code == 404:  # a repository with no commit has no contents
            return "placeholder", license
        if contents.status_code != 200:
            return None
        return ("placeholder" if _filler(_strings(contents.json(), "path")) else "public"), license
    branch = found.get("default_branch") if isinstance(found.get("default_branch"), str) else "HEAD"
    tree = await _github_get(http, f"{named}/git/trees/{quote(branch, safe='')}?recursive=1")
    if tree.status_code == 200:
        if any(_WEIGHTS.search(p) for p in _strings(tree.json().get("tree"), "path")):
            return "public", license
    elif tree.status_code not in (404, 409):  # 404, 409: no commit yet
        return None
    releases = await _github_get(http, f"{named}/releases?per_page=100")
    if releases.status_code != 200:
        return None
    body = releases.json()
    assets = [a for r in body if isinstance(r, dict) for a in _strings(r.get("assets"), "name")]
    return ("public" if any(_WEIGHTS.search(a) for a in assets) else "placeholder"), license


async def _huggingface(http: httpx.AsyncClient, api: str, kind: Kind) -> Checked | None:
    resp = await http.get(api)
    if resp.status_code in (401, 404):  # Hugging Face answers 401 for a repository it has not
        return "missing", None
    if resp.status_code != 200:
        return None
    found = resp.json()
    license = _hf_license(found)
    if found.get("gated") not in (None, False):
        return "gated", license
    names = _strings(found.get("siblings"), "rfilename")
    held = any(_WEIGHTS.search(n) for n in names) if kind == "weights" else not _filler(names)
    return ("public" if held else "placeholder"), license
