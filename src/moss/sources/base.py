"""Base source abstraction for MOSS ingestion.

All ingestion channels (file, email, stdin) implement this interface.
The extractor only consumes RawDoc — it doesn't know or care where
the text came from. This is the core of Axiom A3 (extraction logic
is transport-agnostic).
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class RawDoc:
    """A document ready for extraction.

    Attributes:
        content: Plain text content (already extracted from source format)
        source_type: Origin type (email, file, stdin, etc.)
        source_label: Human-readable label for display
        meta: Additional metadata (sender, subject, file_path, etc.)
        timestamp: When the document was received/created
    """
    content: str
    source_type: str = "unknown"
    source_label: str = ""
    meta: dict = field(default_factory=dict)
    timestamp: Optional[datetime] = None


class BaseSource:
    """Abstract source — subclasses implement fetch()."""

    source_type: str = "base"

    def fetch(self) -> List[RawDoc]:
        """Fetch documents from this source.

        Returns:
            List of RawDoc instances ready for extraction.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} type={self.source_type}>"


def detect_doc_type(content: str, filename: str = "") -> str:
    """Auto-detect document type for skill selection.

    Returns one of: 'weekly-report', 'monthly-report', 'meeting-minutes', 'email', 'default'
    """
    text_lower = content.lower() if content else ""
    name_lower = filename.lower() if filename else ""

    # File name hints
    if any(k in name_lower for k in ["周报", "weekly", "week"]):
        return "weekly-report"
    if any(k in name_lower for k in ["月报", "monthly", "month"]):
        return "monthly-report"
    if any(k in name_lower for k in ["会议", "纪要", "meeting", "minutes"]):
        return "meeting-minutes"

    # Content hints
    if any(k in text_lower for k in ["周报", "本周工作", "下周计划"]):
        return "weekly-report"
    if any(k in text_lower for k in ["月报", "本月工作", "下月计划"]):
        return "monthly-report"
    if any(k in text_lower for k in ["会议纪要", "会议结论", "参会人员", "会议时间"]):
        return "meeting-minutes"

    return "default"
