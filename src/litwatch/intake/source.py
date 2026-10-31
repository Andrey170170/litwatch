"""The seam between intake and wherever papers come from."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from litwatch.intake.http import XmlClient
from litwatch.intake.oai import OaiBatch


class PaperSource(Protocol):
    """Canonical records of one category (or of all, `oai.ALL`) changed from a date to an
    optional last date, with the point they reach.

    `intake.oai` is the implementation; tests and later sources supply another.
    """

    def fetch_category(
        self,
        category: str,
        since: date,
        client: XmlClient,
        *,
        include_versions: bool = False,
        until: date | None = None,
    ) -> OaiBatch: ...
