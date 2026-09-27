import json
import time
from contextlib import contextmanager

import httpx
import pytest
from arxiv_archive.pacing import Cooldown, SharedPacer

from litwatch.arxiv_access import async_get, sync_response


@pytest.mark.asyncio
async def test_shared_async_response_owns_slot_until_close(monkeypatch, tmp_path):
    monkeypatch.setenv("ARXIV_PACING_DIR", str(tmp_path))
    pacer = SharedPacer(tmp_path)
    observed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            with pytest.raises(Cooldown), pacer.slot(wait=False):
                pass
            yield b"%PDF-test"

        async def aclose(self):
            with pytest.raises(Cooldown), pacer.slot(wait=False):
                pass
            observed.append("closed")

    async def handler(request):
        return httpx.Response(200, stream=Stream(), headers={"Retry-After": "90"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await async_get(client, "https://arxiv.org/pdf/2609.00001v1")
    assert response.content == b"%PDF-test"
    assert observed == ["closed"]
    assert pacer.read_cooldown() > time.time() + 85


def test_sync_metadata_retry_after_is_visible_to_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("ARXIV_PACING_DIR", str(tmp_path))
    pacer = SharedPacer(tmp_path)
    closed = []

    class Session:
        @contextmanager
        def get(self, *args, **kwargs):
            assert kwargs["allow_redirects"] is False
            try:
                yield type(
                    "Response", (), {"status_code": 503, "headers": {"Retry-After": "120"}}
                )()
            finally:
                with pytest.raises(Cooldown), pacer.slot(wait=False):
                    pass
                closed.append(True)

    with sync_response(Session(), "https://export.arxiv.org/oai2", stream=True):
        pass
    assert closed == [True]
    state = json.loads((tmp_path / "arxiv-rate.json").read_text())
    assert state["not_before"] > time.time() + 115
