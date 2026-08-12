import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

MAX_EVENTS = 500

_EVENT_FILE: Path | None = None


def _get_event_file() -> Path:
    global _EVENT_FILE
    if _EVENT_FILE is None:
        from moss.config import get_config
        cfg = get_config()
        _EVENT_FILE = cfg.data_path() / "events.jsonl"
    return _EVENT_FILE


def _ensure_file():
    f = _get_event_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        f.touch()


class GovernanceEvent:
    def __init__(self, event_type: str, data: dict, severity: str = "info"):
        self.type = event_type
        self.data = data
        self.timestamp = datetime.now().isoformat()
        self.severity = severity

    def to_dict(self) -> dict:
        return {
            "type": self.type, "data": self.data,
            "timestamp": self.timestamp, "severity": self.severity,
        }

    def to_sse(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, default=str)
        return f"event: {self.type}\ndata: {payload}\n\n"


def load_events(limit: int = 100) -> list[dict]:
    """Load recent events from persistent storage."""
    _ensure_file()
    lines = []
    try:
        with open(_get_event_file(), "r") as f:
            lines = f.readlines()
    except Exception:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line.strip()))
        except json.JSONDecodeError:
            pass
    return events


def _append_event(event: GovernanceEvent):
    _ensure_file()
    line = json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n"
    with open(_get_event_file(), "a") as f:
        f.write(line)
    # Trim if too many
    try:
        with open(_get_event_file(), "r") as f:
            lines = f.readlines()
        if len(lines) > MAX_EVENTS:
            with open(_get_event_file(), "w") as f:
                f.writelines(lines[-MAX_EVENTS:])
    except Exception:
        pass


class EventBus:
    def __init__(self):
        self._queues: list[asyncio.Queue[GovernanceEvent]] = []

    async def emit(self, event_type: str, data: dict, severity: str = "info"):
        event = GovernanceEvent(event_type, data, severity)
        _append_event(event)
        for q in self._queues:
            await q.put(event)

    async def subscribe(self) -> AsyncGenerator[GovernanceEvent, None]:
        q: asyncio.Queue[GovernanceEvent] = asyncio.Queue()
        self._queues.append(q)
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            if q in self._queues:
                self._queues.remove(q)


event_bus = EventBus()


def emit_sync(event_type: str, data: dict, severity: str = "info"):
    """Synchronous event emission (for CLI usage — writes to JSONL file only)."""
    event = GovernanceEvent(event_type, data, severity)
    _append_event(event)
