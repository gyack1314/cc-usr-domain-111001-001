"""采集器端到端检查。

这些检查都会真实启动本地 HTTP 来源（或内嵌 HTTP 服务），并核对落盘的
状态与证据地址，而不是只调用解析函数。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from notice_harvest import collect_round
from notice_harvest.store import StateStore
from source_site.__main__ import running

REPO_ROOT = Path(__file__).resolve().parents[1]


def collect(site, state_dir, round_id, **kwargs):
    return collect_round(
        site=site, state_dir=state_dir, round_id=round_id, **kwargs
    )


def ids(items):
    return [item["source_id"] for item in items]


class CollectorHttpTests(unittest.TestCase):
    def setUp(self):
        self._stack = running()
        self.base, self.source = self._stack.__enter__()
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = self.tempdir.name

    def tearDown(self):
        self.tempdir.cleanup()
        self._stack.__exit__(None, None, None)

    def _store(self):
        return StateStore(self.state_dir, self.base)

    def _state(self):
        store = self._store()
        return json.loads((Path(self.state_dir) / store.state_path).read_text("utf-8"))

    def _assert_page_evidence(self, result):
        for page in result["evidence"]["pages"]:
            json_path = Path(self.state_dir) / page["json_path"]
            self.assertTrue(json_path.exists(), page["json_path"])
            stored = json.loads(json_path.read_text("utf-8"))
            self.assertEqual(stored["json_path"], page["json_path"])
            if page.get("html_path"):
                html_path = Path(self.state_dir) / page["html_path"]
                self.assertTrue(html_path.exists())
                digest = hashlib.sha256(html_path.read_bytes()).hexdigest()
                self.assertEqual(digest, page["body_sha256"])
                self.assertEqual(digest, stored["body_sha256"])
            else:
                self.assertNotEqual(page["status"], 200)

    def _assert_detail_evidence(self, result):
        store = self._store()
        for source_id, meta in result["evidence"]["details"].items():
            json_path = Path(self.state_dir) / meta["json_path"]
            self.assertTrue(json_path.exists())
            stored = json.loads(json_path.read_text("utf-8"))
            if meta.get("html_path"):
                html_path = Path(self.state_dir) / meta["html_path"]
                self.assertTrue(html_path.exists())
                raw = html_path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                self.assertEqual(digest, stored["body_sha256"])
                self.assertEqual(digest, meta["body_sha256"])
                cache = json.loads(
                    (Path(self.state_dir) / store.cache_file(source_id)).read_text(
                        "utf-8"
                    )
                )
                # ETag 与正文必须成对对应，不能混用。
                self.assertEqual(cache["etag"], stored["etag"])
                self.assertEqual(cache["body_sha256"], stored["body_sha256"])

    def test_clean_scan_dedupes_pinned_and_persists_evidence(self):
        result = collect(self.base, self.state_dir, "R1")
        self.assertTrue(result["complete"])
        self.assertEqual(ids(result["changes"]["new"]), ["n1", "n2", "n3"])
        self.assertEqual(result["changes"]["revised"], [])
        self.assertEqual(result["changes"]["withdrawn"], [])
        self.assertEqual(result["changes"]["unconfirmed"], [])
        # n1 同时出现在第 1、2 页（置顶），但只产生一篇。
        self.assertEqual(result["observed"]["pinned_ids"], ["n1"])
        self.assertEqual(result["detail_stats"]["fetched"], 3)
        self._assert_page_evidence(result)
        self._assert_detail_evidence(result)

        state = self._state()
        self.assertEqual(state["seq"], 1)
        self.assertEqual(state["last_round"], "R1")
        n1 = state["records"]["n1"]
        self.assertEqual(n1["title"], "社区消防体验")
        self.assertEqual(n1["body"], "南门体验场地")
        self.assertEqual(n1["time"], "2026-11-12T09:00:00+08:00")
        self.assertTrue(n1["url"].endswith("/detail/n1"))
        self.assertEqual(n1["absent_complete_scans"], 0)

    def test_same_round_is_idempotent_without_new_http_calls(self):
        first = collect(self.base, self.state_dir, "R1")
        calls_before = len(self.source.calls)
        second = collect(self.base, self.state_dir, "R1")
        self.assertEqual(second, first)
        self.assertEqual(len(self.source.calls), calls_before)
        # 轮次文件就是返回结果的持久化副本。
        store = self._store()
        committed = json.loads(
            (Path(self.state_dir) / store.round_file("R1")).read_text("utf-8")
        )
        self.assertEqual(committed["result"], first)

    def test_revision_detects_body_change_through_unchanged_title_and_etag(self):
        collect(self.base, self.state_dir, "R1")
        self.source.phase = "revised"
        result = collect(self.base, self.state_dir, "R2")
        self.assertTrue(result["complete"])
        self.assertEqual(ids(result["changes"]["new"]), [])
        revised = result["changes"]["revised"]
        self.assertEqual(ids(revised), ["n1"])
        self.assertIn("body", revised[0]["changed_fields"])
        self.assertEqual(revised[0]["current"]["body"], "体验场地由南门改到东门")
        # URL 改写不产生第二篇。
        self.assertTrue(revised[0]["current"]["url"].endswith("/detail/n1?print=1"))
        # n1 版本升级，其余详情走 304。
        n1_meta = result["evidence"]["details"]["n1"]
        self.assertEqual(n1_meta["etag"], '"n1-2"')
        self.assertEqual(n1_meta["outcome"], "fetched")
        self.assertEqual(result["detail_stats"]["not_modified"], 2)
        self._assert_detail_evidence(result)

        state = self._state()
        self.assertEqual(state["records"]["n1"]["body"], "体验场地由南门改到东门")
        self.assertEqual(state["records"]["n1"]["etag"], '"n1-2"')

    def test_partial_failure_keeps_evidence_and_does_not_withdraw(self):
        collect(self.base, self.state_dir, "R1")
        self.source.phase = "partial"
        result = collect(self.base, self.state_dir, "R2")
        self.assertFalse(result["complete"])
        self.assertEqual([i["type"] for i in result["issues"]], ["page_fetch_failed"])
        self.assertEqual(result["changes"]["withdrawn"], [])
        unconfirmed = result["changes"]["unconfirmed"]
        self.assertEqual(ids(unconfirmed), ["n3"])
        self.assertEqual(unconfirmed[0]["reason"], "scan_incomplete")
        # 失败页也保留了证据地址。
        failed_pages = [p for p in result["evidence"]["pages"] if p["status"] != 200]
        self.assertTrue(failed_pages)
        self.assertIsNone(failed_pages[0]["html_path"])
        self.assertTrue(
            (Path(self.state_dir) / failed_pages[0]["json_path"]).exists()
        )
        # 缺席计数没有被推进。
        state = self._state()
        self.assertEqual(state["records"]["n3"]["absent_complete_scans"], 0)
        self.assertEqual(state["records"]["n1"]["absent_complete_scans"], 0)

    def test_two_complete_absences_withdraw_and_reappearance_restores(self):
        collect(self.base, self.state_dir, "R1")
        self.source.phase = "missing"
        first = collect(self.base, self.state_dir, "R2-missing-1")
        self.assertTrue(first["complete"])
        self.assertEqual(first["changes"]["withdrawn"], [])
        self.assertEqual(
            [(a["source_id"], a["consecutive_absent_complete_scans"]) for a in first["absent_pending"]],
            [("n1", 1)],
        )
        second = collect(self.base, self.state_dir, "R3-missing-2")
        self.assertEqual(ids(second["changes"]["withdrawn"]), ["n1"])
        withdrawn = second["changes"]["withdrawn"][0]
        self.assertEqual(withdrawn["consecutive_absent_complete_scans"], 2)
        self.assertEqual(withdrawn["current"]["status"], "withdrawn")

        self.source.phase = "base"
        restored = collect(self.base, self.state_dir, "R4-reappear")
        new_items = restored["changes"]["new"]
        self.assertEqual(ids(new_items), ["n1"])
        self.assertTrue(new_items[0]["restored"])
        self.assertEqual(restored["restored_ids"], ["n1"])
        state = self._state()
        self.assertEqual(state["records"]["n1"]["status"], "active")
        self.assertEqual(state["records"]["n1"]["absent_complete_scans"], 0)

    def test_observation_clears_absence_count_during_incomplete_round(self):
        # 先让 n1 缺席一次（计数 1）。
        collect(self.base, self.state_dir, "R1")
        self.source.phase = "missing"
        collect(self.base, self.state_dir, "R2")
        self.assertEqual(self._state()["records"]["n1"]["absent_complete_scans"], 1)
        # 半途失败的轮次里 n1 在第 1 页被实际观察到：计数清零，不能撤下。
        self.source.phase = "partial"
        result = collect(self.base, self.state_dir, "R3")
        self.assertFalse(result["complete"])
        self.assertEqual(result["changes"]["withdrawn"], [])
        self.assertEqual(self._state()["records"]["n1"]["absent_complete_scans"], 0)

    def test_duplicate_cursor_stops_pagination_and_does_not_withdraw(self):
        collect(self.base, self.state_dir, "R1")
        self.source.phase = "loop"
        result = collect(self.base, self.state_dir, "R2")
        self.assertFalse(result["complete"])
        self.assertEqual([i["type"] for i in result["issues"]], ["duplicate_cursor"])
        self.assertEqual(result["changes"]["withdrawn"], [])
        self.assertIn(
            "n3", ids(result["changes"]["unconfirmed"])
        )

    def test_moving_list_change_is_rescanned_and_marked_incomplete(self):
        collect(self.base, self.state_dir, "R1")
        self.source.phase = "moving"
        result = collect(self.base, self.state_dir, "R2")
        self.assertFalse(result["complete"])
        self.assertIn("list_changed", [i["type"] for i in result["issues"]])
        # 补扫捕获了翻页期间插入的 n4。
        self.assertIn("n4", result["observed"]["ids"])
        self.assertEqual(ids(result["changes"]["new"]), ["n4"])
        self.assertEqual(result["changes"]["withdrawn"], [])
        self.assertGreaterEqual(result["observed"]["list_passes"], 3)
        self._assert_detail_evidence(result)
        # 不完整轮次里确认到的新公告仍然落盘。
        self.assertIn("n4", self._state()["records"])

    def test_crash_after_details_recovers_on_same_round(self):
        store = self._store()

        def crash(self_store, round_record):  # noqa: ARG001
            raise RuntimeError("simulated exit before commit")

        with patch.object(StateStore, "commit_round", crash):
            with self.assertRaises(RuntimeError):
                collect(self.base, self.state_dir, "R1")
        self.assertFalse(store.round_file("R1").exists())
        staging = store.staging_dir("R1")
        self.assertEqual(len(list((staging / "details").glob("*.json"))), 3)
        self.assertEqual(len(list((staging / "details").glob("*.html"))), 3)

        recovered = collect(self.base, self.state_dir, "R1")
        self.assertTrue(recovered["complete"])
        self.assertEqual(
            ids(recovered["changes"]["new"]), ["n1", "n2", "n3"]
        )
        # 恢复时用暂存 ETag 发 If-None-Match，全部 304。
        self.assertEqual(recovered["detail_stats"]["not_modified"], 3)
        self.assertEqual(recovered["detail_stats"]["fetched"], 0)
        self.assertTrue(store.round_file("R1").exists())
        again = collect(self.base, self.state_dir, "R1")
        self.assertEqual(again, recovered)

    def test_state_snapshot_rebuilt_after_commit_point(self):
        result = collect(self.base, self.state_dir, "R1")
        state_path = Path(self.state_dir) / result["evidence"]["state"]
        state_path.unlink()
        again = collect(self.base, self.state_dir, "R1")
        self.assertEqual(again, result)
        self.assertTrue(state_path.exists())
        self.assertEqual(json.loads(state_path.read_text("utf-8"))["seq"], 1)

    def test_multiple_sites_are_isolated_under_one_state_dir(self):
        with running() as (other_base, other_source):
            other_source.phase = "missing"
            first = collect(self.base, self.state_dir, "R1")
            second = collect(other_base, self.state_dir, "R1")
            self.assertNotEqual(first["site_key"], second["site_key"])
            self.assertEqual(
                ids(first["changes"]["new"]), ["n1", "n2", "n3"]
            )
            self.assertEqual(ids(second["changes"]["new"]), ["n2", "n3"])
            namespaces = {
                path.name for path in (Path(self.state_dir) / "sites").iterdir()
            }
            self.assertEqual(
                namespaces, {first["site_key"], second["site_key"]}
            )
            # 同轮次文件分别落在两个命名空间，互不覆盖。
            store_a = StateStore(self.state_dir, self.base)
            store_b = StateStore(self.state_dir, other_base)
            self.assertTrue(store_a.round_file("R1").exists())
            self.assertTrue(store_b.round_file("R1").exists())

    def test_cli_subprocess_end_to_end(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "notice_harvest",
                "scan",
                "--site",
                self.base,
                "--state-dir",
                self.state_dir,
                "--round",
                "CLI-1",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["complete"])
        self.assertEqual(ids(result["changes"]["new"]), ["n1", "n2", "n3"])
        calls_before = len(self.source.calls)
        again = subprocess.run(
            [
                sys.executable,
                "-m",
                "notice_harvest",
                "scan",
                "--site",
                self.base,
                "--state-dir",
                self.state_dir,
                "--round",
                "CLI-1",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(again.returncode, 0)
        self.assertEqual(json.loads(again.stdout), result)
        self.assertEqual(len(self.source.calls), calls_before)

    def test_cli_returns_nonzero_with_require_complete_on_failure(self):
        self.source.phase = "partial"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "notice_harvest",
                "scan",
                "--site",
                self.base,
                "--state-dir",
                self.state_dir,
                "--round",
                "CLI-bad",
                "--require-complete",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertEqual(proc.returncode, 1)
        result = json.loads(proc.stdout)
        self.assertFalse(result["complete"])


class Stub304Handler(BaseHTTPRequestHandler):
    """详情无条件返回 304 的边缘来源，用于验证“304 但本地无正文”。"""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/list":
            body = (
                '<html><body><ul>'
                '<li data-id="x1"><a href="/detail/x1">无法确认公告</a></li>'
                '</ul></body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/detail/x1":
            self.send_response(304)
            self.send_header("ETag", '"x1-stub"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class Detail304WithoutBodyTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub304Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_304_without_local_body_is_incomplete_and_unconfirmed(self):
        result = collect(self.base, self.tempdir.name, "edge-304")
        self.assertFalse(result["complete"])
        self.assertEqual(result["issues"][0]["type"], "detail_304_without_body")
        self.assertEqual(
            ids(result["changes"]["unconfirmed"]), ["x1"]
        )
        self.assertEqual(result["changes"]["new"], [])
        # 证据仍然落盘，但没有正文文件。
        meta = result["evidence"]["details"]["x1"]
        self.assertIsNone(meta["html_path"])
        self.assertTrue(
            (Path(self.tempdir.name) / meta["json_path"]).exists()
        )


class StubEtagMismatchHandler(BaseHTTPRequestHandler):
    """详情对任意 If-None-Match 都回 304 但给出不同 ETag 的边缘来源。"""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/list":
            body = (
                '<html><body><ul>'
                '<li data-id="m1"><a href="/detail/m1">ETag 不一致公告</a></li>'
                '</ul></body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/detail/m1":
            if self.headers.get("If-None-Match"):
                self.send_response(304)
                self.send_header("ETag", '"m1-totally-different"')
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = (
                    '<article data-id="m1"><h1>ETag 不一致公告</h1>'
                    '<time>2026-12-02T09:00:00+08:00</time><p>正文</p></article>'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("ETag", '"m1-1"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


class DetailEtagMismatchTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StubEtagMismatchHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_304_etag_mismatch_does_not_reuse_old_body(self):
        # 第一轮正常建立 ETag/正文缓存。
        first = collect(self.base, self.tempdir.name, "round-1")
        self.assertTrue(first["complete"])
        # 第二轮服务器对旧 ETag 回 304 却给出新 ETag：必须拒绝混用。
        second = collect(self.base, self.tempdir.name, "round-2")
        self.assertFalse(second["complete"])
        self.assertEqual(second["issues"][0]["type"], "detail_etag_mismatch")
        self.assertEqual(ids(second["changes"]["unconfirmed"]), ["m1"])
        # 旧缓存原样保留，没有被不一致的 304 覆盖。
        store = StateStore(self.tempdir.name, self.base)
        cache = json.loads(
            (Path(self.tempdir.name) / store.cache_file("m1")).read_text("utf-8")
        )
        self.assertEqual(cache["etag"], '"m1-1"')
        self.assertIn("正文", cache["body"])


class StubFallbackHandler(BaseHTTPRequestHandler):
    """列表给出两个地址，首选 404、备用地址可用，验证按来源编号补取。"""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        split = urlsplit(self.path)
        path = split.path
        if path == "/list":
            body = (
                '<html><body><ul><li data-id="f1">'
                '<a href="/detail/f1?stale=1">备用地址公告</a></li>'
                '<li data-id="f1">'
                '<a href="/detail/f1">备用地址公告</a></li>'
                '</ul></body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/detail/f1" and split.query:
            self.send_response(404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        elif path == "/detail/f1":
            body = (
                '<article data-id="f1"><h1>备用地址公告</h1>'
                '<time>2026-12-03T09:00:00+08:00</time><p>备用正文</p></article>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("ETag", '"f1-1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")


class DetailUrlFallbackTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StubFallbackHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_falls_back_to_alternate_url_without_duplicate_identity(self):
        result = collect(self.base, self.tempdir.name, "fallback")
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual(ids(result["changes"]["new"]), ["f1"])
        meta = result["evidence"]["details"]["f1"]
        self.assertTrue(meta["request_url"].endswith("/detail/f1"))
        self.assertEqual(meta["status"], 200)


if __name__ == "__main__":
    unittest.main()
