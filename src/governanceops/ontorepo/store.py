"""本体版本仓库 (Ontology Repo 的版本化后端)。

复用 Storage 的 SQLite 连接模式与同一个 governance.db，新增 3 处（TBox 是新东西，
与运行时 ABox 的 6 张表并存）：
  onto_versions  — git 式内容寻址版本 (id = content_hash[:12])，含 parent 链
  onto_tags      — 标签 → 版本
  meta['onto.head'] — HEAD 指针（复用现有 meta 表）

刻意不复用 Storage.load() 的"吞异常返回空图"行为：版本缺失/损坏一律显式抛
OntologyStoreError，否则 rollback/diff 会基于损坏数据给出错误结论。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from governanceops.config import get_config

from .model import OntologyModel

_ONTO_SCHEMA = """
CREATE TABLE IF NOT EXISTS onto_versions (
    id TEXT PRIMARY KEY,
    parent TEXT,
    created_at TEXT,
    author TEXT,
    message TEXT,
    content_hash TEXT NOT NULL,
    dsl_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onto_tags (
    tag TEXT PRIMARY KEY,
    version_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_LOCK = threading.Lock()
_HEAD_KEY = "onto.head"


class OntologyStoreError(Exception):
    """版本仓库操作失败（缺失/损坏/无法解析引用）。"""


def content_hash(model: OntologyModel) -> str:
    """基于规范化 DSL（列表按 name 排序）的 sha256；仅顺序不同不改变 hash。"""
    canon = json.dumps(model.canonical_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class OntologyStore:
    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            data_dir = str(get_config().data_path())
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "governance.db"
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_ONTO_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── HEAD ──────────────────────────────────────────────────
    def head(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (_HEAD_KEY,)).fetchone()
            return row["value"] if row else None

    def _set_head(self, conn: sqlite3.Connection, vid: str) -> None:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_HEAD_KEY, vid),
        )

    # ── commit ────────────────────────────────────────────────
    def commit(self, model: OntologyModel, message: str = "build", author: str = "cli") -> str:
        """把当前模型提交为一个版本。内容不变则不新增（返回现有 HEAD）。"""
        chash = content_hash(model)
        vid = chash[:12]
        with _LOCK, self._connect() as conn:
            cur = conn.execute("SELECT value FROM meta WHERE key=?", (_HEAD_KEY,)).fetchone()
            cur_head = cur["value"] if cur else None
            if cur_head:
                hr = conn.execute(
                    "SELECT content_hash FROM onto_versions WHERE id=?", (cur_head,)
                ).fetchone()
                if hr and hr["content_hash"] == chash:
                    return cur_head  # 无变更
            dsl_json = json.dumps(model.canonical_dict(), ensure_ascii=False, sort_keys=True)
            created = datetime.now().isoformat(timespec="seconds")
            exists = conn.execute("SELECT id FROM onto_versions WHERE id=?", (vid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO onto_versions"
                    "(id,parent,created_at,author,message,content_hash,dsl_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (vid, cur_head, created, author, message, chash, dsl_json),
                )
            self._set_head(conn, vid)
            conn.commit()
            return vid

    # ── read ──────────────────────────────────────────────────
    def get(self, version_id: str) -> OntologyModel:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT dsl_json FROM onto_versions WHERE id=?", (version_id,)
            ).fetchone()
        if row is None:
            raise OntologyStoreError(f"版本不存在: {version_id}")
        try:
            return OntologyModel.model_validate(json.loads(row["dsl_json"]))
        except Exception as e:
            raise OntologyStoreError(f"版本 {version_id} 数据损坏: {e}") from e

    def log(self) -> list[dict]:
        """沿 parent 链从 HEAD 回溯。"""
        out: list[dict] = []
        with self._connect() as conn:
            tagmap: dict[str, list[str]] = {}
            for r in conn.execute("SELECT tag, version_id FROM onto_tags"):
                tagmap.setdefault(r["version_id"], []).append(r["tag"])
            vid = self.head()
            seen: set[str] = set()
            while vid and vid not in seen:
                seen.add(vid)
                row = conn.execute("SELECT * FROM onto_versions WHERE id=?", (vid,)).fetchone()
                if row is None:
                    break
                out.append({
                    "id": row["id"], "parent": row["parent"],
                    "created_at": row["created_at"], "author": row["author"],
                    "message": row["message"], "content_hash": row["content_hash"],
                    "tags": tagmap.get(row["id"], []),
                })
                vid = row["parent"]
        return out

    # ── tags / refs ───────────────────────────────────────────
    def tag(self, name: str, version_id: str | None = None) -> str:
        version_id = version_id or self.head()
        if version_id is None:
            raise OntologyStoreError("无 HEAD，无法打标签")
        with _LOCK, self._connect() as conn:
            if conn.execute("SELECT id FROM onto_versions WHERE id=?", (version_id,)).fetchone() is None:
                raise OntologyStoreError(f"版本不存在: {version_id}")
            conn.execute(
                "INSERT INTO onto_tags(tag,version_id) VALUES(?,?) "
                "ON CONFLICT(tag) DO UPDATE SET version_id=excluded.version_id",
                (name, version_id),
            )
            conn.commit()
        return version_id

    def resolve(self, ref: str | None) -> str:
        """tag 名 / 版本 id / 'HEAD' → 版本 id。"""
        if ref in (None, "HEAD", "head"):
            h = self.head()
            if h is None:
                raise OntologyStoreError("无 HEAD")
            return h
        with self._connect() as conn:
            r = conn.execute("SELECT version_id FROM onto_tags WHERE tag=?", (ref,)).fetchone()
            if r:
                return r["version_id"]
            r = conn.execute("SELECT id FROM onto_versions WHERE id=?", (ref,)).fetchone()
            if r:
                return r["id"]
        raise OntologyStoreError(f"无法解析版本引用: {ref}")

    # ── rollback ──────────────────────────────────────────────
    def rollback(self, ref: str, working_path: Path | None = None) -> str:
        """回滚到目标版本：把工作区 DSL 文件还原为该版本内容，并前移 HEAD。

        分类推理读取工作区 DSL 文件，故回滚必须还原文件才能让分类回到旧版。
        """
        target = self.resolve(ref)
        model = self.get(target)  # 显式校验存在且未损坏
        if working_path is not None:
            import yaml
            data = model.model_dump(by_alias=True, exclude_none=True, mode="json")
            Path(working_path).write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        with _LOCK, self._connect() as conn:
            self._set_head(conn, target)
            conn.commit()
        return target
