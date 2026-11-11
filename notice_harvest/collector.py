"""轮次式公告采集的核心逻辑。

轮次完整性
----------
每个轮次至少读取两遍列表（第二遍是确认/补扫）：

* 两遍签名一致：列表在扫描期间没有观察到变化，列表部分完整。
* 签名不一致：继续补扫直到连续两遍一致；即使最终稳定，本轮仍因观察到
  「列表在扫描中变化」而标记为不完整。
* 取页失败、重复游标、达到页数上限：立即判定不完整并停止翻页。
* 详情无法确认（取页异常、304 但本地无对应正文、解析或身份不一致）：
  本轮同样不完整。

不完整轮次保留全部已取得证据，但不推进任何来源编号的缺席计数，也不会
撤下来源。只有连续两次完整扫描都缺席的来源编号才会撤下。
"""
from __future__ import annotations

import base64
import copy
import hashlib
from urllib.parse import urljoin

from .htmlparse import (
    DetailParseError,
    ListParseError,
    content_sha256,
    parse_detail,
    parse_list,
)
from .httpclient import decode_html, fetch, utc_now
from .store import StateStore, list_start_url


class HarvestError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 列表扫描
# ---------------------------------------------------------------------------

def _scan_pass(
    store: StateStore,
    round_id: str,
    pass_no: int,
    timeout: float,
    retries: int,
    max_pages: int,
) -> dict:
    entries: dict[str, dict] = {}
    issues: list[dict] = []
    page_evidence: list[dict] = []
    visited: set[str] = set()
    fatal = False
    signature_parts: list[tuple] = []
    url: str | None = list_start_url(store.site)
    page_index = 0

    while url:
        if url in visited:
            issues.append(
                {"type": "duplicate_cursor", "url": url, "pass": pass_no, "page": page_index}
            )
            fatal = True
            break
        visited.add(url)
        page_index += 1
        if page_index > max_pages:
            issues.append(
                {"type": "page_limit", "url": url, "pass": pass_no, "page": page_index}
            )
            fatal = True
            break

        response = fetch(url, timeout=timeout, retries=retries)
        meta: dict = {
            "pass": pass_no,
            "index": page_index,
            "url": url,
            "status": response.status,
            "fetched_at": response.fetched_at,
            "error": response.error,
            "attempts": response.attempts,
        }
        if response.status != 200:
            meta["outcome"] = "error"
            meta.update(store.write_page_evidence(round_id, pass_no, page_index, meta, None))
            page_evidence.append(meta)
            issues.append(
                {
                    "type": "page_fetch_failed",
                    "url": url,
                    "status": response.status,
                    "error": response.error,
                    "pass": pass_no,
                    "page": page_index,
                }
            )
            fatal = True
            break

        html = decode_html(response.body, response.charset())
        try:
            parsed = parse_list(html, url)
        except ListParseError as exc:
            meta["outcome"] = "parse_error"
            meta["error"] = str(exc)
            meta.update(
                store.write_page_evidence(round_id, pass_no, page_index, meta, response.body)
            )
            page_evidence.append(meta)
            issues.append(
                {
                    "type": "list_parse_failed",
                    "url": url,
                    "error": str(exc),
                    "pass": pass_no,
                    "page": page_index,
                }
            )
            fatal = True
            break

        meta["outcome"] = "ok"
        meta["next_href"] = parsed.next_href
        meta["entries"] = [
            {"source_id": entry.source_id, "url": entry.url, "title": entry.title}
            for entry in parsed.entries
        ]
        meta.update(
            store.write_page_evidence(round_id, pass_no, page_index, meta, response.body)
        )
        page_evidence.append(meta)

        for position, entry in enumerate(parsed.entries):
            destination = entries.setdefault(
                entry.source_id,
                {
                    "source_id": entry.source_id,
                    "url": entry.url,
                    "title": entry.title,
                    "occurrences": [],
                },
            )
            # 同一轮内保留首次出现的地址；跨页重复（置顶）只追加出现位置。
            destination["occurrences"].append(
                {
                    "pass": pass_no,
                    "page": page_index,
                    "position": position,
                    "page_url": url,
                    "url": entry.url,
                    "title": entry.title,
                }
            )

        signature_parts.append(
            (
                url,
                tuple(
                    (entry.source_id, entry.url, entry.title) for entry in parsed.entries
                ),
                parsed.next_href,
            )
        )

        next_url = urljoin(url, parsed.next_href) if parsed.next_href else None
        if next_url and next_url in visited:
            issues.append(
                {
                    "type": "duplicate_cursor",
                    "url": next_url,
                    "from": url,
                    "pass": pass_no,
                    "page": page_index,
                }
            )
            fatal = True
            break
        url = next_url

    return {
        "pass_no": pass_no,
        "entries": entries,
        "page_evidence": page_evidence,
        "issues": issues,
        "fatal": fatal,
        "signature": tuple(signature_parts),
    }


def _scan_lists(
    store: StateStore,
    round_id: str,
    timeout: float,
    retries: int,
    max_rescans: int,
    max_pages: int,
) -> dict:
    entries: dict[str, dict] = {}
    issues: list[dict] = []
    passes: list[dict] = []
    previous_signature: tuple | None = None
    changed = False
    stable = False
    fatal = False

    total_passes = 1 + max(0, max_rescans)
    for pass_no in range(1, total_passes + 1):
        current = _scan_pass(store, round_id, pass_no, timeout, retries, max_pages)
        passes.append(current)
        issues.extend(current["issues"])

        for source_id, entry in current["entries"].items():
            destination = entries.get(source_id)
            if destination is None:
                destination = {
                    "source_id": source_id,
                    "url": entry["url"],
                    "title": entry["title"],
                    "occurrences": [],
                }
                entries[source_id] = destination
            destination["occurrences"].extend(entry["occurrences"])
            # 以最新一遍列表中的首个地址作为详情请求地址。
            destination["url"] = entry["url"]
            destination["title"] = entry["title"]

        if current["fatal"]:
            fatal = True
            break
        if previous_signature is not None:
            if current["signature"] == previous_signature:
                stable = True
                break
            changed = True
        previous_signature = current["signature"]

    if not fatal and not stable:
        issues.append(
            {
                "type": "list_unstable",
                "message": "补扫次数用尽后列表仍未连续两次一致",
                "passes": len(passes),
            }
        )
    if changed:
        issues.append(
            {
                "type": "list_changed",
                "message": "扫描期间观察到列表变化，已合并补扫证据，本轮标记为不完整",
                "passes": len(passes),
            }
        )

    return {
        "entries": entries,
        "passes": passes,
        "issues": issues,
        "changed": changed,
        "stable": stable,
        "fatal": fatal,
    }


# ---------------------------------------------------------------------------
# 详情采集与 ETag 缓存
# ---------------------------------------------------------------------------

def _valid_candidate(record: dict | None) -> dict | None:
    """校验缓存/暂存中的 ETag 与原始正文是否成对且自洽。"""
    if not isinstance(record, dict) or not record.get("etag"):
        return None
    body_b64 = record.get("body_b64")
    if not isinstance(body_b64, str):
        return None
    try:
        raw = base64.b64decode(body_b64, validate=True)
    except (ValueError, TypeError):
        return None
    if hashlib.sha256(raw).hexdigest() != record.get("body_sha256"):
        return None
    return {
        "etag": record["etag"],
        "raw": raw,
        "charset": record.get("charset"),
        "record": record,
    }


def _slim_meta(meta: dict | None) -> dict | None:
    if not isinstance(meta, dict):
        return None
    keys = (
        "source_id",
        "request_url",
        "status",
        "outcome",
        "etag",
        "if_none_match",
        "body_sha256",
        "error",
        "fetched_at",
        "json_path",
        "html_path",
    )
    return {key: meta[key] for key in keys if key in meta}


def _collect_details(
    store: StateStore,
    round_id: str,
    entries: dict[str, dict],
    timeout: float,
    retries: int,
) -> dict:
    issues: list[dict] = []
    verified: dict[str, dict] = {}
    failures: dict[str, dict] = {}
    evidence: dict[str, dict] = {}
    stats = {"fetched": 0, "not_modified": 0, "failed": 0}

    for source_id in sorted(entries):
        entry = entries[source_id]
        # 同一来源编号在列表里可能带不同地址（URL 改写），首选最新一遍的
        # 首个地址，失败时再按出现顺序尝试其余地址。
        candidate_urls: list[str] = []
        for occurrence in entry["occurrences"]:
            alternate = occurrence["url"]
            if alternate not in candidate_urls:
                candidate_urls.append(alternate)
        url = entry["url"]
        # 同一轮次的暂存（崩溃恢复）优先于全局缓存，避免与后续轮次内容混用。
        candidate = _valid_candidate(
            store.load_detail_evidence(round_id, source_id)
        ) or _valid_candidate(store.load_cache(source_id))

        request_headers: dict[str, str] = {}
        if candidate is not None:
            request_headers["If-None-Match"] = candidate["etag"]
        response = fetch(url, headers=request_headers, timeout=timeout, retries=retries)

        if response.status not in (200, 304):
            for alternate in candidate_urls:
                if alternate == url:
                    continue
                retry = fetch(
                    alternate,
                    headers=request_headers,
                    timeout=timeout,
                    retries=retries,
                )
                if retry.status in (200, 304):
                    url = alternate
                    response = retry
                    break

        meta: dict = {
            "source_id": source_id,
            "request_url": url,
            "status": response.status,
            "fetched_at": response.fetched_at,
            "if_none_match": request_headers.get("If-None-Match"),
            "outcome": "error",
            "error": response.error,
            "occurrences": entry["occurrences"],
        }

        if response.status == 200:
            html = decode_html(response.body, response.charset())
            try:
                detail = parse_detail(html, source_id)
            except DetailParseError as exc:
                meta["outcome"] = "parse_error"
                meta["error"] = str(exc)
                paths = store.write_detail_evidence(round_id, source_id, meta, response.body)
                meta.update(paths)
                evidence[source_id] = _slim_meta(meta)
                issue = {
                    "type": "detail_parse_failed",
                    "source_id": source_id,
                    "url": url,
                    "error": str(exc),
                }
                issues.append(issue)
                failures[source_id] = {"reason": issue["type"], "meta": meta}
                stats["failed"] += 1
                continue

            if detail.source_id != source_id:
                meta["outcome"] = "identity_mismatch"
                meta["error"] = (
                    f"列表来源编号 {source_id} 与详情 data-id {detail.source_id} 不一致"
                )
                paths = store.write_detail_evidence(round_id, source_id, meta, response.body)
                meta.update(paths)
                evidence[source_id] = _slim_meta(meta)
                issue = {
                    "type": "detail_identity_mismatch",
                    "source_id": source_id,
                    "url": url,
                    "detail_source_id": detail.source_id,
                }
                issues.append(issue)
                failures[source_id] = {"reason": issue["type"], "meta": meta}
                stats["failed"] += 1
                continue

            etag = response.header("etag")
            fingerprint = content_sha256(detail.title, detail.time, detail.body)
            body_digest = hashlib.sha256(response.body).hexdigest()
            cache_record = {
                "source_id": source_id,
                "etag": etag,
                "url": url,
                "charset": response.charset(),
                "body_b64": base64.b64encode(response.body).decode("ascii"),
                "body_sha256": body_digest,
                "title": detail.title,
                "time": detail.time,
                "body": detail.body,
                "content_sha256": fingerprint,
                "fetched_at": response.fetched_at,
                "round_id": round_id,
            }
            # ETag 与新正文在同一个原子文件中成对写入，不会与旧缓存混用。
            store.save_cache(cache_record)

            meta.update(
                {
                    "outcome": "fetched",
                    "etag": etag,
                    "body_sha256": body_digest,
                    "charset": response.charset(),
                    "title": detail.title,
                    "time": detail.time,
                    "body": detail.body,
                    "content_sha256": fingerprint,
                }
            )
            paths = store.write_detail_evidence(round_id, source_id, meta, response.body)
            meta.update(paths)
            evidence[source_id] = _slim_meta(meta)
            verified[source_id] = {
                "source_id": source_id,
                "title": detail.title,
                "time": detail.time,
                "body": detail.body,
                "url": url,
                "content_sha256": fingerprint,
                "body_sha256": body_digest,
                "etag": etag,
                "evidence": paths["json_path"],
                "fetched_at": response.fetched_at,
            }
            stats["fetched"] += 1

        elif response.status == 304:
            if candidate is None:
                meta["outcome"] = "not_modified_without_body"
                meta["error"] = "来源返回 304，但本地没有对应正文或 ETag 缓存"
                paths = store.write_detail_evidence(round_id, source_id, meta, None)
                meta.update(paths)
                evidence[source_id] = _slim_meta(meta)
                issue = {
                    "type": "detail_304_without_body",
                    "source_id": source_id,
                    "url": url,
                    "status": 304,
                }
                issues.append(issue)
                failures[source_id] = {"reason": issue["type"], "meta": meta}
                stats["failed"] += 1
                continue

            response_etag = response.header("etag")
            if response_etag is not None and response_etag != candidate["etag"]:
                meta["outcome"] = "etag_mismatch"
                meta["error"] = (
                    f"304 响应 ETag {response_etag} 与缓存 ETag {candidate['etag']} 不一致"
                )
                paths = store.write_detail_evidence(round_id, source_id, meta, None)
                meta.update(paths)
                evidence[source_id] = _slim_meta(meta)
                issue = {
                    "type": "detail_etag_mismatch",
                    "source_id": source_id,
                    "url": url,
                    "cached_etag": candidate["etag"],
                    "response_etag": response_etag,
                }
                issues.append(issue)
                failures[source_id] = {"reason": issue["type"], "meta": meta}
                stats["failed"] += 1
                continue

            raw = candidate["raw"]
            html = decode_html(raw, candidate["charset"])
            try:
                detail = parse_detail(html, source_id)
            except DetailParseError as exc:
                meta["outcome"] = "parse_error"
                meta["error"] = str(exc)
                paths = store.write_detail_evidence(round_id, source_id, meta, None)
                meta.update(paths)
                evidence[source_id] = _slim_meta(meta)
                issue = {
                    "type": "detail_parse_failed",
                    "source_id": source_id,
                    "url": url,
                    "error": str(exc),
                }
                issues.append(issue)
                failures[source_id] = {"reason": issue["type"], "meta": meta}
                stats["failed"] += 1
                continue

            if detail.source_id != source_id:
                meta["outcome"] = "identity_mismatch"
                paths = store.write_detail_evidence(round_id, source_id, meta, None)
                meta.update(paths)
                evidence[source_id] = _slim_meta(meta)
                issue = {
                    "type": "detail_identity_mismatch",
                    "source_id": source_id,
                    "url": url,
                    "detail_source_id": detail.source_id,
                }
                issues.append(issue)
                failures[source_id] = {"reason": issue["type"], "meta": meta}
                stats["failed"] += 1
                continue

            fingerprint = content_sha256(detail.title, detail.time, detail.body)
            body_digest = hashlib.sha256(raw).hexdigest()
            # 暂存恢复时全局缓存可能缺失：用与来源 ETag 成对的正文补齐。
            cache_record = {
                "source_id": source_id,
                "etag": candidate["etag"],
                "url": url,
                "charset": candidate["charset"],
                "body_b64": base64.b64encode(raw).decode("ascii"),
                "body_sha256": body_digest,
                "title": detail.title,
                "time": detail.time,
                "body": detail.body,
                "content_sha256": fingerprint,
                "fetched_at": response.fetched_at,
                "round_id": round_id,
            }
            store.save_cache(cache_record)

            meta.update(
                {
                    "outcome": "not_modified",
                    "etag": candidate["etag"],
                    "body_sha256": body_digest,
                    "charset": candidate["charset"],
                    "title": detail.title,
                    "time": detail.time,
                    "body": detail.body,
                    "content_sha256": fingerprint,
                }
            )
            paths = store.write_detail_evidence(round_id, source_id, meta, raw)
            meta.update(paths)
            evidence[source_id] = _slim_meta(meta)
            verified[source_id] = {
                "source_id": source_id,
                "title": detail.title,
                "time": detail.time,
                "body": detail.body,
                "url": url,
                "content_sha256": fingerprint,
                "body_sha256": body_digest,
                "etag": candidate["etag"],
                "evidence": paths["json_path"],
                "fetched_at": response.fetched_at,
            }
            stats["not_modified"] += 1

        else:
            meta["outcome"] = "http_error"
            paths = store.write_detail_evidence(round_id, source_id, meta, None)
            meta.update(paths)
            evidence[source_id] = _slim_meta(meta)
            issue = {
                "type": "detail_fetch_failed",
                "source_id": source_id,
                "url": url,
                "status": response.status,
                "error": response.error,
            }
            issues.append(issue)
            failures[source_id] = {"reason": issue["type"], "meta": meta}
            stats["failed"] += 1

    return {
        "verified": verified,
        "failures": failures,
        "evidence": evidence,
        "issues": issues,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# 状态变化计算
# ---------------------------------------------------------------------------

_PUBLIC_KEYS = (
    "source_id",
    "status",
    "absent_complete_scans",
    "title",
    "time",
    "body",
    "url",
    "content_sha256",
    "body_sha256",
    "etag",
    "first_seen_round",
    "last_observed_round",
    "last_verified_round",
    "last_evidence",
)


def _public(record: dict | None) -> dict | None:
    if record is None:
        return None
    return {key: record.get(key) for key in _PUBLIC_KEYS}


def _new_record(verified: dict, round_id: str, now: str) -> dict:
    return {
        "source_id": verified["source_id"],
        "status": "active",
        "absent_complete_scans": 0,
        "title": verified["title"],
        "time": verified["time"],
        "body": verified["body"],
        "url": verified["url"],
        "content_sha256": verified["content_sha256"],
        "body_sha256": verified.get("body_sha256"),
        "etag": verified.get("etag"),
        "first_seen_round": round_id,
        "first_seen_at": now,
        "last_observed_round": round_id,
        "last_observed_at": now,
        "last_verified_round": round_id,
        "last_evidence": verified["evidence"],
        "restoration_pending": False,
    }


def _refresh_record(old: dict, verified: dict, round_id: str, now: str) -> dict:
    updated = copy.deepcopy(old)
    updated.update(
        {
            "status": "active",
            "absent_complete_scans": 0,
            "title": verified["title"],
            "time": verified["time"],
            "body": verified["body"],
            "url": verified["url"],
            "content_sha256": verified["content_sha256"],
            "body_sha256": verified.get("body_sha256"),
            "etag": verified.get("etag"),
            "last_observed_round": round_id,
            "last_observed_at": now,
            "last_verified_round": round_id,
            "last_evidence": verified["evidence"],
        }
    )
    return updated


def _apply_changes(
    previous: dict,
    scan: dict,
    details: dict,
    complete: bool,
    round_id: str,
) -> tuple[dict, dict]:
    records: dict[str, dict] = copy.deepcopy(previous.get("records", {}))
    verified = details["verified"]
    failures = details["failures"]
    observed_ids = set(scan["entries"])

    new: list[dict] = []
    revised: list[dict] = []
    withdrawn: list[dict] = []
    unconfirmed: list[dict] = []
    absent_pending: list[dict] = []
    url_changes: list[dict] = []
    restored_ids: list[str] = []
    now = utc_now()

    # 1) 详情确认成功的来源编号：新增、恢复或修订，并清缺席计数。
    for source_id in sorted(verified):
        current = verified[source_id]
        old = records.get(source_id)
        if old is None:
            record = _new_record(current, round_id, now)
            records[source_id] = record
            item = _public(record)
            item["restored"] = False
            new.append(item)
            continue

        restored = (
            old.get("status") == "withdrawn" or bool(old.get("restoration_pending"))
        )
        content_changed = old.get("content_sha256") != current["content_sha256"]
        changed_fields = [
            field
            for field in ("title", "time", "body")
            if old.get(field) != current[field]
        ]
        url_changed = old.get("url") != current["url"]

        updated = _refresh_record(old, current, round_id, now)
        updated["restoration_pending"] = False
        records[source_id] = updated

        if restored:
            restored_ids.append(source_id)
            item = _public(updated)
            item["restored"] = True
            item["previous"] = _public(old)
            new.append(item)
        elif content_changed:
            if url_changed:
                changed_fields.append("url")
            revised.append(
                {
                    "source_id": source_id,
                    "reason": "content_changed",
                    "changed_fields": changed_fields,
                    "previous": _public(old),
                    "current": _public(updated),
                    "title": updated["title"],
                    "time": updated["time"],
                    "body": updated["body"],
                    "url": updated["url"],
                    "content_sha256": updated["content_sha256"],
                    "evidence": current["evidence"],
                }
            )
        elif url_changed:
            # URL 改写不产生第二篇，也不单独算正文修订。
            url_changes.append(
                {
                    "source_id": source_id,
                    "previous_url": old.get("url"),
                    "current_url": updated["url"],
                    "evidence": current["evidence"],
                }
            )

    # 2) 列表观察到但详情无法确认：保留旧记录、清缺席计数，但列入无法确认。
    for source_id in sorted(observed_ids - set(verified)):
        old = records.get(source_id)
        failure = failures.get(source_id, {})
        reason = failure.get("reason", "detail_unconfirmed")
        meta = failure.get("meta")
        previous_snapshot = _public(old)
        restored = False
        if old is not None:
            # 确实观察到记录：清掉缺席计数。
            old["absent_complete_scans"] = 0
            old["last_observed_round"] = round_id
            old["last_observed_at"] = now
            if old.get("status") == "withdrawn":
                old["status"] = "active"
                old["restoration_pending"] = True
                restored = True
                restored_ids.append(source_id)
            records[source_id] = old
        unconfirmed.append(
            {
                "source_id": source_id,
                "reason": reason,
                "observed_in_list": True,
                "restored": restored,
                "detail": _slim_meta(meta),
                "previous": previous_snapshot,
                "evidence": meta.get("json_path") if isinstance(meta, dict) else None,
            }
        )

    # 3) 列表中完全没有出现的已知来源编号。
    for source_id in sorted(set(records) - set(verified) - observed_ids):
        old = records[source_id]
        if not complete:
            # 不完整轮次不推进缺席计数，更不能撤下。
            unconfirmed.append(
                {
                    "source_id": source_id,
                    "reason": "scan_incomplete",
                    "observed_in_list": False,
                    "restored": False,
                    "detail": None,
                    "previous": _public(old),
                    "evidence": None,
                }
            )
            continue

        if old.get("status") == "withdrawn":
            # 已撤下的记录保持撤下，不重复报告、不重复计数。
            continue

        before = _public(old)
        count = min(int(old.get("absent_complete_scans", 0)) + 1, 2)
        old["absent_complete_scans"] = count
        old["last_absent_round"] = round_id
        if count >= 2:
            old["status"] = "withdrawn"
            withdrawn.append(
                {
                    "source_id": source_id,
                    "reason": "absent_in_two_consecutive_complete_scans",
                    "consecutive_absent_complete_scans": count,
                    "previous": before,
                    "current": _public(old),
                }
            )
        else:
            absent_pending.append(
                {
                    "source_id": source_id,
                    "consecutive_absent_complete_scans": count,
                    "previous": before,
                }
            )

    changes = {
        "new": new,
        "revised": revised,
        "withdrawn": withdrawn,
        "unconfirmed": unconfirmed,
    }
    extras = {
        "absent_pending": absent_pending,
        "restored_ids": sorted(restored_ids),
        "url_changes": url_changes,
    }
    return records, {"changes": changes, **extras}


# ---------------------------------------------------------------------------
# 轮次入口
# ---------------------------------------------------------------------------

def _pinned_ids(entries: dict[str, dict]) -> list[str]:
    pinned: list[str] = []
    for source_id, entry in sorted(entries.items()):
        pages_by_pass: dict[int, set[int]] = {}
        for occurrence in entry["occurrences"]:
            pages_by_pass.setdefault(occurrence["pass"], set()).add(occurrence["page"])
        if any(len(pages) > 1 for pages in pages_by_pass.values()):
            pinned.append(source_id)
    return pinned


def collect_round(
    *,
    site: str,
    state_dir: str,
    round_id: str,
    timeout: float = 10.0,
    retries: int = 0,
    max_rescans: int = 2,
    max_pages: int = 100,
) -> dict:
    """执行一轮采集并提交结果。

    同一个 ``round_id`` 重复执行会直接返回已提交的相同结果，不再发请求；
    进程在详情已保存但轮次未提交时退出，下次以同一 ``round_id`` 重跑即可
    恢复。
    """
    if round_id is None or not str(round_id).strip():
        raise HarvestError("轮次标识不能为空")
    round_id = str(round_id)
    if max_rescans < 1:
        raise HarvestError("max_rescans 至少为 1，否则无法确认列表是否稳定")

    store = StateStore(state_dir, site)
    with store.lock():
        existing = store.load_round(round_id)
        if existing is not None:
            # 幂等：同一轮次不重扫、不重算，返回完全相同的结果。
            store.ensure_state_current()
            return existing["result"]

        previous = store.load_state()
        started_at = utc_now()
        manifest = {
            "schema": 1,
            "round_id": round_id,
            "round_key": store.round_key(round_id),
            "site": store.site,
            "site_key": store.site_key,
            "status": "scanning",
            "started_at": started_at,
            "updated_at": started_at,
        }
        store.save_manifest(round_id, manifest)

        scan = _scan_lists(
            store, round_id, timeout, retries, max_rescans, max_pages
        )
        manifest["status"] = "collecting_details"
        manifest["list"] = {
            "passes": len(scan["passes"]),
            "stable": scan["stable"],
            "changed": scan["changed"],
            "fatal": scan["fatal"],
            "observed_ids": sorted(scan["entries"]),
            "issues": scan["issues"],
        }
        manifest["updated_at"] = utc_now()
        store.save_manifest(round_id, manifest)

        details = _collect_details(
            store, round_id, scan["entries"], timeout, retries
        )

        issues = list(scan["issues"]) + list(details["issues"])
        complete = not issues
        records, change_bundle = _apply_changes(
            previous, scan, details, complete, round_id
        )
        committed_at = utc_now()

        seq = int(previous.get("seq", 0)) + 1
        new_state = {
            "schema": 1,
            "site": store.site,
            "site_key": store.site_key,
            "seq": seq,
            "last_round": round_id,
            "records": records,
            "updated_at": committed_at,
        }

        page_evidence: list[dict] = []
        for passed in scan["passes"]:
            page_evidence.extend(passed["page_evidence"])

        result = {
            "round_id": round_id,
            "round_key": store.round_key(round_id),
            "site": store.site,
            "site_key": store.site_key,
            "seq": seq,
            "previous_round": previous.get("last_round"),
            "complete": complete,
            "issues": issues,
            "changes": change_bundle["changes"],
            "absent_pending": change_bundle["absent_pending"],
            "restored_ids": change_bundle["restored_ids"],
            "url_changes": change_bundle["url_changes"],
            "observed": {
                "ids": sorted(scan["entries"]),
                "count": len(scan["entries"]),
                "occurrences": {
                    source_id: len(entry["occurrences"])
                    for source_id, entry in sorted(scan["entries"].items())
                },
                "pinned_ids": _pinned_ids(scan["entries"]),
                "list_passes": len(scan["passes"]),
                "stable": scan["stable"],
                "changed": scan["changed"],
            },
            "detail_stats": details["stats"],
            "evidence": {
                "site_state_dir": store.rel(store.root),
                "manifest": store.rel(store.manifest_path(round_id)),
                "pages": page_evidence,
                "details": {
                    source_id: _slim_meta(meta)
                    for source_id, meta in sorted(details["evidence"].items())
                },
                "state": store.rel(store.state_path),
                "round": store.rel(store.round_file(round_id)),
            },
            "state_summary": {
                "records": len(records),
                "active": sum(1 for item in records.values() if item.get("status") == "active"),
                "withdrawn": sum(
                    1 for item in records.values() if item.get("status") == "withdrawn"
                ),
            },
            "started_at": started_at,
            "committed_at": committed_at,
        }

        manifest["status"] = "ready_to_commit"
        manifest["complete"] = complete
        manifest["issues"] = issues
        manifest["detail_stats"] = details["stats"]
        manifest["updated_at"] = utc_now()
        store.save_manifest(round_id, manifest)

        round_record = {
            "schema": 1,
            "round_id": round_id,
            "round_key": store.round_key(round_id),
            "site": store.site,
            "site_key": store.site_key,
            "seq": seq,
            "committed_at": committed_at,
            "result": result,
            "state_after": new_state,
        }
        store.commit_round(round_record)
        return result


__all__ = ["HarvestError", "collect_round"]
