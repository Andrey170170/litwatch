"""The re-check of a placeholder link, by a plain request to its host's API, not by the agent: a
GitHub repository (`api.github.com/repos/<owner>/<repo>` and its contents) or a Hugging Face
model, dataset or space (`huggingface.co/api/<kind>s/<owner>/<name>`). A link on any other host
is not checked again (`checkable`). GitHub's redirects (a renamed repository) are followed within
its API. A Hugging Face weights link holds its weights only when the repository lists a weight
file; a README and a config alone are a placeholder. An Adapter over today's HTTP client until
the Fetch gate (A5)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from litwatch.core.links import Kind, State

log = logging.getLogger(__name__)

TIMEOUT = 30.0
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
    host not checked."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if host == "github.com" and len(path) >= 2:
        return f"https://api.github.com/repos/{path[0]}/{path[1].removesuffix('.git')}", "github"
    if host == "huggingface.co" and path:
        kind, rest = (path[0], path[1:]) if path[0] in ("datasets", "spaces") else ("models", path)
        if len(rest) >= 2:
            return f"https://huggingface.co/api/{kind}/{rest[0]}/{rest[1]}", "huggingface"
    return None


def checkable(url: str) -> bool:
    """Whether a re-check can say what the link holds: one on GitHub or Hugging Face."""
    return _target(url) is not None


def _filler(names: list[str]) -> bool:
    return all(_FILLER.match(n.rsplit("/", 1)[-1]) for n in names)


def _github_license(repo: dict[str, Any]) -> str | None:
    found = repo.get("license") or {}
    spdx = found.get("spdx_id")
    return spdx if spdx and spdx != "NOASSERTION" else found.get("name")


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
    """What the `kind` link holds now: missing (the host has no such repository), a placeholder
    (code: nothing but a README beside its filler; weights: no weight file), gated (Hugging
    Face's gate), or public; None when the host could not say (it is not checked here, did not
    answer, refused, or was rate limited)."""
    target = _target(url)
    if target is None:
        return None
    api, host = target
    try:
        async with client() as http:
            if host == "github":
                return await _github(http, api)
            return await _huggingface(http, api, kind)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("re-checking %s: %s could not say: %s", url, host, exc)
        return None


async def _github_get(http: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET `url`, following GitHub's redirects (a renamed or moved repository) within its API,
    `REDIRECTS` at most."""
    resp = await http.get(url)
    for _ in range(REDIRECTS):
        if not resp.is_redirect or "location" not in resp.headers:
            break
        moved = resp.url.join(resp.headers["location"])
        if moved.scheme != "https" or moved.host != "api.github.com":
            break
        resp = await http.get(moved)
    return resp


async def _github(http: httpx.AsyncClient, api: str) -> Checked | None:
    repo = await _github_get(http, api)
    if repo.status_code == 404:
        return "missing", None
    if repo.status_code != 200:
        return None
    found = repo.json()
    license = _github_license(found)
    # Its contents, at the repository as GitHub now names it.
    contents = await _github_get(
        http, f"https://api.github.com/repos/{found['full_name']}/contents"
    )
    if contents.status_code == 404:  # a repository with no commit has no contents
        return "placeholder", license
    if contents.status_code != 200:
        return None
    names = [e.get("path") or e.get("name") or "" for e in contents.json()]
    return ("placeholder" if _filler(names) else "public"), license


async def _huggingface(http: httpx.AsyncClient, api: str, kind: Kind) -> Checked | None:
    resp = await http.get(api)
    if resp.status_code in (401, 404):  # Hugging Face answers 401 for a repository it has not
        return "missing", None
    if resp.status_code != 200:
        return None
    found = resp.json()
    license = _hf_license(found)
    names = [s.get("rfilename", "") for s in found.get("siblings") or []]
    held = any(_WEIGHTS.search(n) for n in names) if kind == "weights" else not _filler(names)
    if not held:
        return "placeholder", license
    return ("gated" if found.get("gated") else "public"), license
