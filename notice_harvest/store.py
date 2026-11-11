"""采集状态与证据的持久化层。

目录布局（每个来源在同一个状态目录下相互隔离）::

    <state-dir>/sites/<site-key>/
        state.json                 # 已提交的最新状态快照
        details/<id>.json          # 按来源编号缓存的 ETag + 原始正文（单文件原子成对）
        rounds/<round-key>/       # 轮次暂存：清单、列表页、详情证据
        rounds/<round-key>.json    # 已提交轮次（结果 + state_after，单文件提交点）
        .lock

所有写入都经过临时文件 + ``os.replace``，保证进程在提交点前后退出都不会
留下半截 JSON。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from .httpclient import utc_now

SCHEMA_VERSION = 1


class HarvestStoreError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 名称与路径
# ---------------------------------------------------------------------------

def normalize_site(site: str) -> str:
    if not isinstance(site, str) or not site.strip():
        raise ValueError("站点地址不能为空")
    parts = urlsplit(site.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(f"站点地址必须是 http(s) URL: {site!r}")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def list_start_url(site: str) -> str:
    normalized = normalize_site(site)
    return urljoin(normalized + "/", "list?page=1")


def _safe_component(value: str) -> str:
    text = str(value)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._") or "x"
    if len(text) <= 80 and cleaned == text:
        return cleaned
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:70]}-{digest}"


def site_key(normalized_site: str) -> str:
    netloc = urlsplit(normalized_site).netloc
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", netloc)[:40] or "site"
    digest = hashlib.sha256(normalized_site.encode("utf-8")).hexdigest()[:10]
    return f"{label}-{digest}"


def source_filename(source_id: str) -> str:
    return _safe_component(source_id)


def round_filename(round_id: str) -> str:
    return _safe_component(round_id)


# ---------------------------------------------------------------------------
# 原子写入
# ---------------------------------------------------------------------------

def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    # 目录 fsync 失败不影响语义，某些文件系统也不支持。
    with contextlib.suppress(OSError):
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def dump_json(obj: object) -> bytes:
    return (
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def atomic_write_json(path: Path, obj: object) -> None:
    atomic_write_bytes(path, dump_json(obj))


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 状态存储
# ---------------------------------------------------------------------------

class StateStore:
    def __init__(self, state_dir: str | os.PathLike[str], site: str) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.site = normalize_site(site)
        self.site_key = site_key(self.site)
        self.root = self.state_dir / "sites" / self.site_key
        self.details_dir = self.root / "details"
        self.rounds_dir = self.root / "rounds"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".lock"
        for directory in (self.root, self.details_dir, self.rounds_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def rel(self, path: str | os.PathLike[str]) -> str:
        return Path(path).resolve().relative_to(self.state_dir).as_posix()

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as handle:
            try:
                import fcntl  # POSIX；运行环境为 Linux。

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - 非 POSIX 兜底
                pass
            try:
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except ImportError:  # pragma: no cover
                    pass

    # -- 已提交轮次 -------------------------------------------------------

    def round_key(self, round_id: str) -> str:
        return round_filename(round_id)

    def round_file(self, round_id: str) -> Path:
        return self.rounds_dir / f"{self.round_key(round_id)}.json"

    def staging_dir(self, round_id: str) -> Path:
        return self.rounds_dir / self.round_key(round_id)

    def load_round(self, round_id: str) -> dict | None:
        path = self.round_file(round_id)
        if not path.exists():
            return None
        data = read_json(path)
        if not isinstance(data, dict) or data.get("round_id") != round_id:
            raise HarvestStoreError(f"轮次文件身份不一致: {path}")
        if data.get("site_key") != self.site_key:
            raise HarvestStoreError(f"轮次文件属于其他来源: {path}")
        return data

    def _committed_rounds(self) -> list[dict]:
        records: list[dict] = []
        for path in sorted(self.rounds_dir.glob("*.json")):
            try:
                data = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("site_key") == self.site_key:
                records.append(data)
        return records

    def latest_committed(self) -> dict | None:
        rounds = self._committed_rounds()
        if not rounds:
            return None
        return max(rounds, key=lambda item: int(item.get("seq", -1)))

    def initial_state(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "site": self.site,
            "site_key": self.site_key,
            "seq": 0,
            "last_round": None,
            "records": {},
            "updated_at": utc_now(),
        }

    def _read_state(self) -> dict | None:
        if not self.state_path.exists():
            return None
        data = read_json(self.state_path)
        if not isinstance(data, dict) or data.get("site_key") != self.site_key:
            raise HarvestStoreError(f"状态文件身份不一致: {self.state_path}")
        return data

    def load_state(self) -> dict:
        """返回当前状态；state.json 落后于已提交轮次时以轮次文件为准。"""
        state = self._read_state()
        latest = self.latest_committed()
        if latest is not None and int(latest.get("seq", -1)) > int(
            (state or {}).get("seq", -1)
        ):
            state = latest.get("state_after")
        if state is None:
            state = self.initial_state()
        state.setdefault("records", {})
        return state

    def ensure_state_current(self) -> None:
        """提交点已落盘但 state.json 未写完时，从轮次文件重建快照。"""
        state = self.load_state()
        disk = self._read_state()
        if disk is None or int(disk.get("seq", -1)) < int(state.get("seq", -1)):
            atomic_write_json(self.state_path, state)

    def write_state(self, state: dict) -> None:
        atomic_write_json(self.state_path, state)

    def commit_round(self, round_record: dict) -> None:
        """提交点：先原子写轮次文件，再写可重建的状态快照。"""
        path = self.round_file(round_record["round_id"])
        atomic_write_json(path, round_record)
        self.write_state(round_record["state_after"])
        manifest = self.load_manifest(round_record["round_id"])
        if manifest is not None:
            manifest["status"] = "committed"
            manifest["committed_at"] = round_record.get("committed_at")
            manifest["updated_at"] = utc_now()
            self.save_manifest(round_record["round_id"], manifest)

    # -- 轮次暂存清单 -----------------------------------------------------

    def manifest_path(self, round_id: str) -> Path:
        return self.staging_dir(round_id) / "manifest.json"

    def load_manifest(self, round_id: str) -> dict | None:
        path = self.manifest_path(round_id)
        if not path.exists():
            return None
        data = read_json(path)
        return data if isinstance(data, dict) else None

    def save_manifest(self, round_id: str, manifest: dict) -> None:
        atomic_write_json(self.manifest_path(round_id), manifest)

    # -- 列表页证据 -------------------------------------------------------

    def write_page_evidence(
        self,
        round_id: str,
        pass_no: int,
        page_index: int,
        meta: dict,
        raw: bytes | None,
    ) -> dict[str, str | None]:
        directory = self.staging_dir(round_id) / "pages"
        stem = f"pass{pass_no:02d}-page{page_index:02d}"
        json_path = directory / f"{stem}.json"
        html_path = directory / f"{stem}.html"
        stored = dict(meta)
        if raw is not None:
            digest = hashlib.sha256(raw).hexdigest()
            stored["body_sha256"] = digest
        else:
            digest = None
        stored["json_path"] = self.rel(json_path)
        stored["html_path"] = self.rel(html_path) if raw is not None else None
        atomic_write_json(json_path, stored)
        if raw is not None:
            atomic_write_bytes(html_path, raw)
        elif html_path.exists():
            with contextlib.suppress(OSError):
                html_path.unlink()
        return {
            "json_path": stored["json_path"],
            "html_path": stored["html_path"],
            "body_sha256": digest,
        }

    # -- 详情缓存（ETag 与正文成对，不可混用） -----------------------------

    def cache_file(self, source_id: str) -> Path:
        return self.details_dir / f"{source_filename(source_id)}.json"

    def load_cache(self, source_id: str) -> dict | None:
        path = self.cache_file(source_id)
        if not path.exists():
            return None
        data = read_json(path)
        if not isinstance(data, dict) or data.get("source_id") != source_id:
            return None
        return data

    def save_cache(self, record: dict) -> None:
        atomic_write_json(self.cache_file(record["source_id"]), record)

    # -- 轮次详情证据 -----------------------------------------------------

    def detail_evidence_paths(self, round_id: str, source_id: str) -> tuple[Path, Path]:
        directory = self.staging_dir(round_id) / "details"
        stem = source_filename(source_id)
        return directory / f"{stem}.json", directory / f"{stem}.html"

    def load_detail_evidence(self, round_id: str, source_id: str) -> dict | None:
        json_path, _ = self.detail_evidence_paths(round_id, source_id)
        if not json_path.exists():
            return None
        data = read_json(json_path)
        if not isinstance(data, dict) or data.get("source_id") != source_id:
            return None
        body_b64 = data.get("body_b64")
        if body_b64 is None:
            return None
        try:
            raw = base64.b64decode(body_b64, validate=True)
        except (ValueError, TypeError):
            return None
        if hashlib.sha256(raw).hexdigest() != data.get("body_sha256"):
            return None
        return data

    def write_detail_evidence(
        self,
        round_id: str,
        source_id: str,
        meta: dict,
        raw: bytes | None,
    ) -> dict[str, str | None]:
        json_path, html_path = self.detail_evidence_paths(round_id, source_id)
        stored = dict(meta)
        if raw is not None:
            stored["body_b64"] = base64.b64encode(raw).decode("ascii")
            stored["body_sha256"] = hashlib.sha256(raw).hexdigest()
        else:
            stored.pop("body_b64", None)
            stored.pop("body_sha256", None)
        stored["json_path"] = self.rel(json_path)
        stored["html_path"] = self.rel(html_path) if raw is not None else None
        atomic_write_json(json_path, stored)
        if raw is not None:
            atomic_write_bytes(html_path, raw)
        elif html_path.exists():
            with contextlib.suppress(OSError):
                html_path.unlink()
        return {"json_path": stored["json_path"], "html_path": stored["html_path"]}
