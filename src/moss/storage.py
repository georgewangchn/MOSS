"""Storage layer for MOSS.

Default backend: SQLite (cross-platform, concurrent-safe, zero external service).
Fallback: JSON file (for export/import and legacy compatibility).

SQLite schema mirrors the GovernanceGraph model exactly — each table
corresponds to a Pydantic model field collection.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from moss.config import get_config
from moss.models import (
    Assignment,
    Decision,
    GovernanceGraph,
    Issue,
    Update,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT DEFAULT 'ingest',
    issue_id TEXT,
    owner TEXT,
    department TEXT,
    dependency TEXT,
    depends_on TEXT DEFAULT '[]',
    created_at TEXT,
    deadline TEXT,
    status TEXT DEFAULT 'OPEN',
    progress REAL DEFAULT 0.0,
    risk_level TEXT DEFAULT 'LOW',
    last_update_at TEXT,
    raw_statement TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    severity TEXT DEFAULT 'MEDIUM',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS updates (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    content TEXT,
    progress REAL,
    risk TEXT,
    created_at TEXT,
    source TEXT DEFAULT 'cli'
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    department TEXT,
    assigned_at TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_DB_LOCK = threading.Lock()
_logger = logging.getLogger("moss.storage")


class Storage:
    """SQLite-backed storage with JSON export capability."""

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            cfg = get_config()
            data_dir = str(cfg.data_path())
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "governance.db"
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring pre-existing databases up to the current schema.

        CREATE TABLE IF NOT EXISTS won't add columns to an old table,
        so additive columns are backfilled here (idempotent).
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
        if "depends_on" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN depends_on TEXT DEFAULT '[]'")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def save(self, graph: GovernanceGraph) -> None:
        """Persist governance graph to SQLite (full replace)."""
        with _DB_LOCK, self._connect() as conn:
            # Clear existing data
            for table in ("decisions", "issues", "updates", "assignments", "edges"):
                conn.execute(f"DELETE FROM {table}")

            # Insert decisions
            for did, d in graph.decisions.items():
                conn.execute(
                    """INSERT INTO decisions
                    (id, title, source, issue_id, owner, department, dependency,
                     depends_on, created_at, deadline, status, progress, risk_level,
                     last_update_at, raw_statement)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (did, d.title, d.source, d.issue_id, d.owner, d.department,
                     d.dependency,
                     json.dumps(d.depends_on),
                     d.created_at.isoformat() if d.created_at else None,
                     d.deadline.isoformat() if d.deadline else None,
                     d.status, d.progress, d.risk_level,
                     d.last_update_at.isoformat() if d.last_update_at else None,
                     d.raw_statement),
                )

            # Insert issues
            for iid, issue in graph.issues.items():
                conn.execute(
                    """INSERT INTO issues (id, title, description, severity, created_at)
                    VALUES (?,?,?,?,?)""",
                    (iid, issue.title, issue.description, issue.severity,
                     issue.created_at.isoformat() if issue.created_at else None),
                )

            # Insert updates
            for u in graph.updates:
                conn.execute(
                    """INSERT INTO updates
                    (id, decision_id, content, progress, risk, created_at, source)
                    VALUES (?,?,?,?,?,?,?)""",
                    (u.id, u.decision_id, u.content, u.progress, u.risk,
                     u.created_at.isoformat() if u.created_at else None, u.source),
                )

            # Insert assignments
            for a in graph.assignments:
                conn.execute(
                    """INSERT INTO assignments
                    (decision_id, owner, department, assigned_at)
                    VALUES (?,?,?,?)""",
                    (a.decision_id, a.owner, a.department,
                     a.assigned_at.isoformat() if a.assigned_at else None),
                )

            # Insert edges
            for edge_type, src_map in graph.edges.items():
                for src_id, targets in src_map.items():
                    for tgt_id in targets:
                        conn.execute(
                            "INSERT INTO edges (edge_type, source_id, target_id) VALUES (?,?,?)",
                            (edge_type, src_id, tgt_id),
                        )

            conn.commit()

    def load(self) -> GovernanceGraph:
        """Load governance graph from SQLite."""
        if not self.db_path.exists():
            return GovernanceGraph()

        try:
            with self._connect() as conn:
                decisions = {}
                for row in conn.execute("SELECT * FROM decisions"):
                    decisions[row["id"]] = Decision(
                        id=row["id"],
                        title=row["title"],
                        source=row["source"] or "ingest",
                        issue_id=row["issue_id"],
                        owner=row["owner"],
                        department=row["department"],
                        dependency=row["dependency"],
                        depends_on=_parse_json_list(row["depends_on"]),
                        created_at=_parse_dt(row["created_at"]),
                        deadline=_parse_dt(row["deadline"]),
                        status=row["status"] or "OPEN",
                        progress=row["progress"] or 0.0,
                        risk_level=row["risk_level"] or "LOW",
                        last_update_at=_parse_dt(row["last_update_at"]),
                        raw_statement=row["raw_statement"] or "",
                    )

                issues = {}
                for row in conn.execute("SELECT * FROM issues"):
                    issues[row["id"]] = Issue(
                        id=row["id"],
                        title=row["title"],
                        description=row["description"] or "",
                        severity=row["severity"] or "MEDIUM",
                        created_at=_parse_dt(row["created_at"]),
                    )

                updates = []
                for row in conn.execute("SELECT * FROM updates ORDER BY created_at"):
                    updates.append(Update(
                        id=row["id"],
                        decision_id=row["decision_id"],
                        content=row["content"] or "",
                        progress=row["progress"],
                        risk=row["risk"],
                        created_at=_parse_dt(row["created_at"]),
                        source=row["source"] or "cli",
                    ))

                assignments = []
                for row in conn.execute("SELECT * FROM assignments ORDER BY assigned_at"):
                    assignments.append(Assignment(
                        decision_id=row["decision_id"],
                        owner=row["owner"],
                        department=row["department"],
                        assigned_at=_parse_dt(row["assigned_at"]),
                    ))

                edges: dict[str, dict[str, list[str]]] = {
                    "issue_to_decision": {},
                    "decision_to_updates": {},
                    "decision_to_assignment": {},
                }
                for row in conn.execute("SELECT * FROM edges"):
                    etype = row["edge_type"]
                    if etype not in edges:
                        edges[etype] = {}
                    src = row["source_id"]
                    tgt = row["target_id"]
                    edges[etype].setdefault(src, []).append(tgt)

                return GovernanceGraph(
                    decisions=decisions,
                    issues=issues,
                    updates=updates,
                    assignments=assignments,
                    edges=edges,
                )
        except Exception as e:
            _logger.error("Failed to load governance graph from %s: %s", self.db_path, e, exc_info=True)
            return GovernanceGraph()

    def exists(self) -> bool:
        return self.db_path.exists()

    def export_json(self, path: str | Path | None = None) -> Path:
        """Export current graph as JSON file (legacy compatibility)."""
        graph = self.load()
        if path is None:
            path = self.data_dir / "governance_graph.json"
        path = Path(path)
        data = graph.model_dump(mode="json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        return path

    def import_json(self, path: str | Path) -> None:
        """Import graph from JSON file (migration / legacy data)."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"JSON file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        graph = GovernanceGraph.model_validate(data)
        self.save(graph)


def _parse_dt(val: str | None) -> datetime | None:
    """Parse ISO datetime string, return None if empty."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _parse_json_list(val: str | None) -> list:
    """Parse JSON-encoded string list (e.g. depends_on), return [] if empty/invalid."""
    if not val:
        return []
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []
