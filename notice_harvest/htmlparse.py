"""列表页与详情页的最小 HTML 解析。

来源页面的结构由 sources/contract.json 约定：

* 列表页每页两项，``<li data-id="..."><a href="...">标题</a></li>``，
  ``<a rel="next" href="...">`` 指向下一页。
* 详情页为 ``<article data-id="..."><h1>标题</h1><time>时间</time>
  <p>正文</p></article>``。

解析器只负责把可核对字段取出来；页面身份、去重、轮次完整性等语义在
collector 中处理。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

_WHITESPACE = re.compile(r"\s+")


def collapse(text: str | None) -> str:
    """压缩空白，避免 HTML 换行影响字段比较。"""
    return _WHITESPACE.sub(" ", text or "").strip()


def content_sha256(title: str, time: str, body: str) -> str:
    """内容指纹：标题未变也不会掩盖正文变化。"""
    payload = json.dumps(
        {"title": title, "time": time, "body": body},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ListEntry:
    source_id: str
    url: str
    title: str


@dataclass
class ListPage:
    url: str
    entries: list[ListEntry]
    next_href: str | None


@dataclass(frozen=True)
class Detail:
    source_id: str
    title: str
    time: str
    body: str


class ListParseError(ValueError):
    pass


class DetailParseError(ValueError):
    pass


def _derive_source_id(href: str | None) -> str | None:
    if not href:
        return None
    path = urlsplit(href).path
    match = re.search(r"/detail/([^/]+)/?$", path)
    return unquote(match.group(1)) if match else None


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[ListEntry] = []
        self.next_href: str | None = None
        self._li: dict | None = None
        self._anchor: dict | None = None
        self._li_text: list[str] = []

    def _finish_li(self) -> None:
        if self._li is None:
            return
        href = self._anchor["href"] if self._anchor else None
        source_id = self._li.get("data_id") or _derive_source_id(href)
        if source_id and href:
            title = collapse("".join(self._anchor["text"])) if self._anchor else ""
            if not title:
                title = collapse("".join(self._li_text))
            self.entries.append(ListEntry(source_id=source_id, url=href, title=title))
        self._li = None
        self._anchor = None
        self._li_text = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value for key, value in attrs}
        if tag == "a":
            rel = (attributes.get("rel") or "").lower().split()
            if "next" in rel and self.next_href is None and attributes.get("href"):
                self.next_href = attributes["href"]
            if self._li is not None and self._anchor is None:
                self._anchor = {"href": attributes.get("href"), "text": []}
        elif tag == "li":
            # 兼容缺少 </li> 的页面：遇到新 li 时先结算上一个。
            self._finish_li()
            self._li = {"data_id": attributes.get("data-id")}
            self._anchor = None
            self._li_text = []
        elif tag == "br" and self._anchor is not None:
            self._anchor["text"].append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._li is not None:
            self._li_text.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            # 锚点归属于当前 li，在 </li> 结算时一起处理；避免清空后丢失条目。
            return
        if tag == "li":
            self._finish_li()


def parse_list(html: str, page_url: str) -> ListPage:
    parser = _ListParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # HTMLParser 对畸形输入也可能抛错
        raise ListParseError(str(exc)) from exc
    entries = [
        ListEntry(
            source_id=entry.source_id,
            url=urljoin(page_url, entry.url),
            title=entry.title,
        )
        for entry in parser.entries
    ]
    return ListPage(url=page_url, entries=entries, next_href=parser.next_href)


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.article_id: str | None = None
        self.in_article = False
        self._text_tag: str | None = None
        self._buffer: list[str] = []
        self.title_parts: list[str] = []
        self.time_parts: list[str] = []
        self.body_parts: list[str] = []

    def _flush(self) -> None:
        if self._text_tag is None:
            return
        text = "".join(self._buffer)
        if self._text_tag == "h1":
            self.title_parts.append(text)
        elif self._text_tag == "time":
            self.time_parts.append(text)
        else:
            self.body_parts.append(text)
        self._text_tag = None
        self._buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value for key, value in attrs}
        if tag == "article":
            self.in_article = True
            if self.article_id is None and attributes.get("data-id"):
                self.article_id = attributes["data-id"]
            return
        if not self.in_article:
            return
        if tag in ("h1", "time", "p"):
            self._flush()
            self._text_tag = tag
            self._buffer = []
        elif tag == "br" and self._text_tag is not None:
            self._buffer.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.in_article and self._text_tag is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._text_tag:
            self._flush()
        if tag == "article":
            self._flush()
            self.in_article = False


def parse_detail(html: str, requested_id: str) -> Detail:
    parser = _DetailParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise DetailParseError(str(exc)) from exc
    parser._flush()

    title = collapse(" ".join(parser.title_parts))
    time = collapse(" ".join(parser.time_parts))
    paragraphs = [collapse(part) for part in parser.body_parts]
    body = "\n".join(part for part in paragraphs if part)
    source_id = parser.article_id or requested_id

    if not (parser.title_parts or parser.time_parts or parser.body_parts):
        raise DetailParseError("详情页缺少标题、时间与正文")
    if not title:
        raise DetailParseError("详情页缺少标题")
    return Detail(source_id=source_id, title=title, time=time, body=body)
