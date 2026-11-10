"""来源样本和本地HTTP服务的回归检查。"""
import hashlib
import json
import unittest
from urllib.request import urlopen
from source_site.source import Source
from source_site.__main__ import running


class SourceTests(unittest.TestCase):
    def test_source_1(self):
        s = Source()
        self.assertIn(b"n1", s.request("GET", "/list", {"page": ["1"]})[2])

    def test_source_2(self):
        s = Source()
        first = s.request("GET", "/detail/n1")
        self.assertEqual(304, s.request("GET", "/detail/n1", headers={"if-none-match": first[1]["ETag"]})[0])

    def test_source_3(self):
        s = Source()
        s.phase = "partial"
        self.assertEqual(503, s.request("GET", "/list", {"page": ["2"]})[0])

    def test_source_4(self):
        s = Source()
        s.phase = "missing"
        self.assertEqual(404, s.request("GET", "/detail/n1")[0])

    def test_http_source(self):
        with running() as (base, source):
            with urlopen(base + "/list", timeout=3) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read())


if __name__ == "__main__":
    unittest.main()
