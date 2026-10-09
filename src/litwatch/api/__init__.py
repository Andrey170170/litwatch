"""HTTP API. For now only what labelling for the evaluation needs; the full API and the web
app come with milestone 3."""

from litwatch.api.app import create_app

__all__ = ["create_app"]
