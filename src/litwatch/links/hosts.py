"""The re-check of a placeholder link, by a plain request to its host's API, not by the agent: a
GitHub repository (`api.github.com/repos/<owner>/<repo>` and its contents) or a Hugging Face
model, dataset or space (`huggingface.co/api/<kind>s/<owner>/<name>`). A link on any other host
is not checked again (`checkable`). An Adapter over today's HTTP client until the Fetch gate
(A5)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from litwatch.core.links import State

log = logging.getLogger(__name__)

TIMEOUT = 30.0
# What a repository holds beside its code when it holds no code yet: a README, a licence, its
# git and GitHub files, a citation.
_FILLER = re.compile(
    r"^(readme|licen[cs]e|copying|citation|notice)(\.\w+)?$|^\.git(ignore|attributes|hub)?$",
    re.IGNORECASE,
)

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


async def check(url: str, http: httpx.AsyncClient | None = None) -> Checked | None:
    """What the link holds now: missing (the host has no such repository), a placeholder
    (nothing but a README beside its filler), gated (Hugging Face's gate), or public; None when
    the host could not say (it is not checked here, did not answer, refused, or was rate
    limited)."""
    target = _target(url)
    if target is None:
        return None
    api, host = target
    try:
        if http is not None:
            return await (_github if host == "github" else _huggingface)(http, api)
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"user-agent": "litwatch"}) as own:
            return await (_github if host == "github" else _huggingface)(own, api)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("re-checking %s: %s could not say: %s", url, host, exc)
        return None


async def _github(http: httpx.AsyncClient, api: str) -> Checked | None:
    repo = await http.get(api)
    if repo.status_code == 404:
        return "missing", None
    if repo.status_code != 200:
        return None
    license = _github_license(repo.json())
    contents = await http.get(f"{api}/contents")
    if contents.status_code == 404:  # a repository with no commit has no contents
        return "placeholder", license
    if contents.status_code != 200:
        return None
    names = [e.get("path") or e.get("name") or "" for e in contents.json()]
    return ("placeholder" if _filler(names) else "public"), license


async def _huggingface(http: httpx.AsyncClient, api: str) -> Checked | None:
    resp = await http.get(api)
    if resp.status_code in (401, 404):  # Hugging Face answers 401 for a repository it has not
        return "missing", None
    if resp.status_code != 200:
        return None
    found = resp.json()
    license = _hf_license(found)
    names = [s.get("rfilename", "") for s in found.get("siblings") or []]
    if _filler(names):
        return "placeholder", license
    return ("gated" if found.get("gated") else "public"), license
