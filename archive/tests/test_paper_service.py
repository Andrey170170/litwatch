"""HTTP boundaries: auth, bounded writes, readback and queue promotion."""
import http.client
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from arxiv_archive.api import PaperServer, read_token
from arxiv_archive.store import Store

from test_paper_store import tar_bytes

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work, self.archive = self.root / "work", self.root / "archive"
        self.work.mkdir()
        self.archive.mkdir()
        self.store = Store(work=self.work, archive=self.archive, guard=False)
        self.token = "test-token-" + "a" * 32
        self.server = PaperServer(("127.0.0.1", 0), self.store, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.catalog.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None, auth=True):
        request_headers = {"Authorization": "Bearer " + self.token} if auth else {}
        request_headers.update(headers or {})
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            client.request(method, path, body=body, headers=request_headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_authentication_and_private_token(self):
        status, _, body = self.request("GET", "/v1/status", auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn(self.token.encode(), body)
        status, _, _ = self.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        path = self.root / "token"
        path.write_text(self.token)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_token(path)
        path.chmod(0o600)
        self.assertEqual(read_token(path), self.token)

    def test_pdf_source_write_read_and_old_style_id(self):
        for paper_id, kind, original in (("2609.12345", "pdf", PDF),
                                         ("hep-th/9901001", "source", b"\\documentclass{article}\n\\begin{document}Hi\\end{document}")):
            route = f"/v1/papers/{paper_id}/{kind}?version=1"
            with patch("arxiv_archive.api.shutil.disk_usage") as disk:
                disk.return_value.free = 20 * 1024**3
                status, _, body = self.request("PUT", route, original)
            self.assertEqual(status, 200, body)
            status, headers, body = self.request("GET", route)
            self.assertEqual(status, 200, body)
            if kind == "pdf":
                self.assertEqual(body, original)
                self.assertEqual(headers["Content-Type"], "application/pdf")
            else:
                # Sources come back as the stored text-only package.
                with tarfile.open(fileobj=io.BytesIO(body)) as tar:
                    self.assertEqual(tar.extractfile("main.tex").read(), original)
        self.assertFalse(list(self.store.scratch.glob("http-upload-*")))

    def test_text_route(self):
        source = self.root / "input"
        source.write_bytes(
            tar_bytes(
                [("main.tex", b"\\documentclass{article}\\begin{document}Served\\end{document}")]
            )
        )
        self.store.put("2609.12345", 2, "source", source)
        status, _, body = self.request("GET", "/v1/papers/2609.12345/text?version=2")
        self.assertEqual(status, 200, body)
        answer = json.loads(body)
        self.assertEqual(
            (answer["paper_id"], answer["version"], answer["source"]), ("2609.12345", 2, "latex")
        )
        self.assertIn("Served", answer["text"])
        status, _, _ = self.request("GET", "/v1/papers/2609.12345/text?version=1")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/v1/papers/2609.99999/text")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/v1/papers/2609.12345/text", auth=False)
        self.assertEqual(status, 401)

    def test_rejects_oversized_unversioned_and_corrupt_upload(self):
        status, _, _ = self.request("PUT", "/v1/papers/2609.12345/pdf", PDF)
        self.assertEqual(status, 400)
        status, _, _ = self.request("PUT", "/v1/papers/2609.12345/pdf?version=1", b"",
                                     {"Content-Length": str(self.store.max_bytes + 1)})
        self.assertEqual(status, 413)
        with patch("arxiv_archive.api.shutil.disk_usage") as disk:
            disk.return_value.free = 20 * 1024**3
            status, _, _ = self.request("PUT", "/v1/papers/2609.12345/pdf?version=1", b"not a PDF")
        self.assertEqual(status, 400)
        self.assertFalse(list(self.store.scratch.glob("http-upload-*")))
        status, _, _ = self.request("GET", "/v1/papers/2609.12345/pdf")
        self.assertEqual(status, 404)

    def test_metadata_and_durable_priority_promotion(self):
        paper = {"metadata": {"title": "Test paper"}, "latest_version": 2}
        status, _, _ = self.request("PUT", "/v1/papers/2609.12345", json.dumps(paper))
        self.assertEqual(status, 200)
        status, _, body = self.request("GET", "/v1/papers/2609.12345")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["metadata"]["title"], "Test paper")
        self.assertFalse(json.loads(body)["has_historical_source"])
        with self.store.catalog.lock, self.store.catalog.db:
            self.store.catalog.db.execute("INSERT INTO latex_locations VALUES (?,?,?)",
                                         ("2609.12345", "hf/latex/test.parquet", 0))
        status, _, body = self.request("GET", "/v1/papers/2609.12345")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["has_historical_source"])
        job = {"paper_id": "2609.12345", "version": 2, "kind": "pdf", "priority": 100}
        status, _, body = self.request("POST", "/v1/jobs", json.dumps(job))
        self.assertEqual(status, 202, body)
        initial = json.loads(body)
        job["priority"] = 0
        status, _, body = self.request("POST", "/v1/jobs", json.dumps(job))
        self.assertEqual(status, 202, body)
        urgent = json.loads(body)
        self.assertEqual(initial["id"], urgent["id"])
        self.assertEqual(urgent["priority"], 0)
        status, _, body = self.request("GET", "/v1/jobs/" + str(urgent["id"]))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["priority"], 0)
        claimed = self.store.catalog.claim_job()
        self.assertTrue(self.store.catalog.finish_job(claimed["id"], claimed["lease_token"],
                                                    status="failed", error="transient download failure"))
        status, _, body = self.request("POST", "/v1/jobs/" + str(urgent["id"]) + "/retry", "{}")
        self.assertEqual(status, 202, body)
        retried = json.loads(body)
        self.assertEqual(retried["status"], "pending")
        self.assertEqual(retried["attempts"], 0)
        self.assertIsNone(retried["error"])

    def test_lists_papers_by_window_in_pages(self):
        for paper_id, day, category in (("2610.00001", "2026-10-01", "cs.CL"),
                                        ("hep-th/9901001", "2026-10-02", "cs.CL"),
                                        ("2610.00003", "2026-10-02", "cs.AI"),
                                        ("2610.00004", "2026-10-09", "cs.CL")):
            self.store.catalog.upsert_paper(paper_id, {"title": paper_id, "published": day,
                                                       "categories": [category]}, 1)
        route = "/v1/papers?since=2026-10-01&until=2026-10-08&categories=cs.CL,cs.LG&limit=1"
        status, _, _ = self.request("GET", route, auth=False)
        self.assertEqual(status, 401)
        ids, cursor = [], ""
        while True:
            status, _, body = self.request("GET", route + cursor)
            self.assertEqual(status, 200, body)
            page = json.loads(body)
            ids += [item["id"] for item in page["items"]]
            if page["next"] is None:
                break
            cursor = "&cursor=" + quote(page["next"])
        self.assertEqual(ids, ["2610.00001", "hep-th/9901001"])
        for bad in ("since=2026-10-01", "since=2026-10-08&until=2026-10-01",
                    "since=2026-10-01&until=2026-10-08&limit=0",
                    "since=2026-10-01&until=2026-10-08&categories=cs.CL;drop",
                    "since=2026-10-01&until=2026-10-08&cursor=nonsense",
                    "since=2026-10-01&until=2026-10-08&q=x"):
            status, _, _ = self.request("GET", "/v1/papers?" + bad)
            self.assertEqual(status, 400, bad)
        with self.store.catalog.lock, self.store.catalog.db:
            self.store.catalog.db.execute("DELETE FROM import_checkpoints")
        status, _, body = self.request("GET", route)
        self.assertEqual(status, 503)
        self.assertIn(b"index-dates", body)

    def test_concurrency_bound_rejects_before_body(self):
        for _ in range(4):
            self.assertTrue(self.server.slots.acquire(blocking=False))
        try:
            status, headers, _ = self.request("GET", "/v1/status")
            self.assertEqual(status, 503)
            self.assertEqual(headers["Retry-After"], "2")
        finally:
            for _ in range(4):
                self.server.slots.release()


if __name__ == "__main__":
    unittest.main()
