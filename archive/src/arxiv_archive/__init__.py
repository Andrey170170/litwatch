"""Shared paper catalog, artifact store and acquisition service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("arxiv-archive")
except PackageNotFoundError:  # run from a source tree that was never installed
    __version__ = "0.0.0"
