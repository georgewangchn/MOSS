"""File source — read documents from local files.

Supports: .md, .txt, .docx, .pdf
For unsupported formats, falls back to raw text read.
"""

from pathlib import Path
from typing import List, Optional

from moss.sources.base import BaseSource, RawDoc


class FileSource(BaseSource):
    """Read one or more files as RawDoc instances."""

    source_type = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"File not found: {self.path}")

    def fetch(self) -> List[RawDoc]:
        suffix = self.path.suffix.lower()
        if suffix == ".docx":
            content = self._read_docx()
        elif suffix == ".pdf":
            content = self._read_pdf()
        elif suffix in (".md", ".txt", ".markdown"):
            content = self._read_text()
        else:
            # Fallback: try reading as text
            content = self._read_text()

        if not content.strip():
            return []

        return [RawDoc(
            content=content,
            source_type=self.source_type,
            source_label=str(self.path.name),
            meta={"file_path": str(self.path), "file_type": suffix},
        )]

    def _read_text(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")

    def _read_docx(self) -> str:
        """Extract text from .docx using python-docx."""
        try:
            from docx import Document
            doc = Document(str(self.path))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            # Also extract tables
            for table in doc.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        paragraphs.append(" | ".join(cells))
            return "\n".join(paragraphs)
        except ImportError:
            print("[FileSource] python-docx not installed, trying raw text read")
            return self._read_text()
        except Exception as e:
            print(f"[FileSource] docx read failed: {e}")
            return self._read_text()

    def _read_pdf(self) -> str:
        """Extract text from .pdf using pypdf."""
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(self.path))
            pages = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
        except ImportError:
            print("[FileSource] pypdf not installed, trying raw text read")
            return self._read_text()
        except Exception as e:
            print(f"[FileSource] pdf read failed: {e}")
            return self._read_text()


class DirectorySource(BaseSource):
    """Scan a directory for supported files."""

    source_type = "directory"

    SUPPORTED_EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".markdown"}

    def __init__(self, dir_path: str | Path, recursive: bool = False):
        self.dir_path = Path(dir_path)
        self.recursive = recursive
        if not self.dir_path.is_dir():
            raise NotADirectoryError(f"Directory not found: {self.dir_path}")

    def fetch(self) -> List[RawDoc]:
        pattern = "**/*" if self.recursive else "*"
        docs = []
        for f in self.dir_path.glob(pattern):
            if f.is_file() and f.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                try:
                    source = FileSource(f)
                    docs.extend(source.fetch())
                except Exception as e:
                    print(f"[DirectorySource] Skip {f}: {e}")
        return docs
