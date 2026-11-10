"""合成来源行为，可直接调用或通过本地 HTTP 访问。"""
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs

DATA = Path(__file__).resolve().parents[1] / "sources"


def json_file(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class Source:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []
        self.phase = "base"
        self.state = {}

    def request(self, method, path, query=None, headers=None, body=b""):
        query, headers = query or {}, headers or {}
        self.calls.append({"method": method, "path": path, "query": query})
        if path == "/__control" and method == "POST":
            control = json.loads(body or b"{}")
            self.phase = control.get("phase", self.phase)
            self.state.update(control.get("state", {}))
            return 200, {}, {"phase": self.phase}
        if path == "/__calls":
            return 200, {}, self.calls[:-1]
        return self.dispatch(method, path, query, headers, body)

    def dispatch(self, method, path, q, h, body):
        records = json_file("notices.json")
        inserted = {"id": "n4", "title": "新增活动", "time": "2026-11-15T09:00:00+08:00", "body": "合成新活动公告", "version": 1, "url": "/detail/n4"}
        if self.phase == "moving" and self.state.get("inserted"):
            records.insert(0, inserted)
        if self.phase == "revised":
            records[0]["body"] = "体验场地由南门改到东门"
            records[0]["version"] = 2
            records[0]["url"] = "/detail/n1?print=1"
        if self.phase == "missing":
            records = [r for r in records if r["id"] != "n1"]
        if path == "/list":
            page = int(q.get("page", ["1"])[0])
            if self.phase == "partial" and page == 2:
                return 503, {}, {"error": "栏目临时不可读"}
            if self.phase == "moving" and page > 1 and not self.state.get("inserted"):
                self.state["inserted"] = True
                records.insert(0, inserted)
            part = records[(page-1)*2:page*2]
            links = "".join('<li data-id="{id}"><a href="{url}">{title}</a></li>'.format(**r) for r in part)
            nxt = (page if self.phase == "loop" else page+1) if page*2 < len(records) else None
            html = '<html><body><ul>' + links + '</ul>' + (f'<a rel="next" href="/list?page={nxt}">下一页</a>' if nxt else '') + '</body></html>'
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html.encode()
        if path.startswith("/detail/"):
            key = path.rsplit("/", 1)[-1]
            row = next((r for r in records if r["id"] == key), None)
            if not row:
                return 404, {}, {"error": "公告不存在"}
            etag = '"' + key + '-' + str(row["version"]) + '"'
            if h.get("if-none-match") == etag:
                return 304, {"ETag": etag}, b""
            html = f'<article data-id="{key}"><h1>{row["title"]}</h1><time>{row["time"]}</time><p>{row["body"]}</p></article>'
            return 200, {"Content-Type": "text/html; charset=utf-8", "ETag": etag}, html.encode()
        return 404, {}, {"error": "路径不存在"}
