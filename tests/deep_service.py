"""The deep service's side: a finished card job about `Q` (`DONE`), and the Deep integration
on a fake service (`make`)."""

from litwatch.integrations.deep import Deep, DeepClient
from tests.conftest import (
    paper,
)
from tests.fakes_http import FakeDeep, mock

Q = paper("2610.00002", title="Probes for hidden steps", abstract="Linear probes.", version=1)
USAGE = {"turns": 7, "input": 41000, "output": 2300, "list_cost": 0.42, "cells": 4}
CARD = {
    "summary": "S.",
    "key_claim": "K.",
    "among_collection": [
        {"arxiv_id": Q.id, "title": "Probes", "relation": "extends", "note": "Same probes."},
        {"arxiv_id": "2501.00009", "title": "Elsewhere", "relation": "agrees"},
        {"arxiv_id": "2501.00010", "title": "Unknown kind", "relation": "cites"},
    ],
}
DONE = {"result": CARD, "stats": USAGE, "model": "deep-model", "trace": []}


def make(fake: FakeDeep, **kw) -> Deep:
    client = DeepClient("http://deep.test", http=mock(fake.handle), poll_seconds=0)
    return Deep(client, **kw)
