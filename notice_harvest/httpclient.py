"""只依赖标准库的 HTTP 客户端。

304 在 urllib 中会抛出 ``HTTPError``，这里统一转成 :class:`HttpResult`，
由上层依据 ETag 与本地缓存决定如何使用。
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message

_CHARSET = re.compile(r"charset=([^\s;]+)", re.IGNORECASE)
USER_AGENT = "notice-harvest/1.0 (+local fire-safety notice collector)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def header_charset(headers: dict[str, str]) -> str | None:
    content_type = headers.get("content-type", "")
    match = _CHARSET.search(content_type or "")
    return match.group(1).lower() if match else None


def decode_html(body: bytes, charset: str | None = None) -> str:
    candidates = [c for c in (charset, "utf-8", "gb18030", "latin-1") if c]
    for candidate in candidates:
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _lower_headers(headers: Message | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


@dataclass
class HttpResult:
    url: str
    status: int | None
    headers: dict[str, str]
    body: bytes
    error: str | None
    fetched_at: str
    attempts: int = 1

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def charset(self) -> str | None:
        return header_charset(self.headers)


def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    retries: int = 0,
) -> HttpResult:
    """发起一次 GET。

    网络异常（非 HTTP 状态码）最多重试 ``retries`` 次；HTTP 状态码（包括
    304/4xx/5xx）不重试，直接作为采集证据返回。
    """
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }
    request_headers.update(headers or {})
    last_error: Exception | None = None

    for attempt in range(max(0, retries) + 1):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResult(
                    url=url,
                    status=response.status,
                    headers=_lower_headers(response.headers),
                    body=response.read(),
                    error=None,
                    fetched_at=utc_now(),
                    attempts=attempt + 1,
                )
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except (OSError, ValueError):
                body = b""
            return HttpResult(
                url=url,
                status=exc.code,
                headers=_lower_headers(exc.headers),
                body=body,
                error=None,
                fetched_at=utc_now(),
                attempts=attempt + 1,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(0.2 * (attempt + 1), 1.0))
                continue

    return HttpResult(
        url=url,
        status=None,
        headers={},
        body=b"",
        error=repr(last_error) if last_error is not None else "network error",
        fetched_at=utc_now(),
        attempts=max(0, retries) + 1,
    )
