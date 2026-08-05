"""Email source — fetch documents from IMAP mailboxes.

Default provider: IMAP (works with Exchange, Gmail, QQ Mail, etc.)
Future: GraphSource (O365), GmailSource (OAuth) as optional subclasses.

IMAP is prioritized over OAuth because:
  - Most enterprise email systems support IMAP
  - No OAuth app registration required
  - Password/App-password auth is simpler for initial setup
  (Axiom A2: first run should not require complex external setup)
"""

import email
import imaplib
import os
import sys
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional

from governanceops.sources.base import BaseSource, RawDoc
from governanceops.sources.file_source import FileSource


def _decode_header(raw: str) -> str:
    """Decode RFC 2047 encoded email header."""
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return "".join(decoded)


def _extract_body(msg: email.message.Message) -> str:
    """Extract text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace") if payload else ""
        # Fallback: try HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace") if payload else ""
        return ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""


def _extract_attachments(msg: email.message.Message, tmp_dir: Path) -> List[Path]:
    """Extract supported attachments to tmp dir, return file paths."""
    attachments = []
    supported = {".md", ".txt", ".docx", ".pdf"}
    for part in msg.walk():
        cd = str(part.get("Content-Disposition", ""))
        if "attachment" in cd:
            filename = _decode_header(part.get_filename() or "")
            if not filename:
                continue
            suffix = Path(filename).suffix.lower()
            if suffix not in supported:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            tmp_dir.mkdir(parents=True, exist_ok=True)
            file_path = tmp_dir / filename
            file_path.write_bytes(payload)
            attachments.append(file_path)
    return attachments


class EmailSource(BaseSource):
    """Fetch emails from an IMAP mailbox.

    Config (from .env or init):
        IMAP_HOST: IMAP server hostname
        IMAP_PORT: IMAP server port (default 993)
        IMAP_USER: Email account
        IMAP_PASS: Password or app-specific password
        IMAP_FOLDER: Mailbox folder (default INBOX)
        IMAP_SINCE_HOURS: Only fetch emails from last N hours (default 24)
    """

    source_type = "email"

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        folder: Optional[str] = None,
        since_hours: int = 24,
    ):
        self.host = host or os.getenv("IMAP_HOST", "")
        self.port = port or int(os.getenv("IMAP_PORT", "993"))
        self.user = user or os.getenv("IMAP_USER", "")
        self.password = password or os.getenv("IMAP_PASS", "")
        self.folder = folder or os.getenv("IMAP_FOLDER", "INBOX")
        self.since_hours = since_hours

        if not self.host or not self.user or not self.password:
            raise ValueError(
                "IMAP not configured. Run: governance-ops connect email --provider imap"
            )

    def fetch(self) -> List[RawDoc]:
        """Fetch recent emails and return as RawDoc list."""
        docs: List[RawDoc] = []
        tmp_dir = Path.home() / ".governance-ops" / "tmp" / "attachments"

        try:
            mail = imaplib.IMAP4_SSL(self.host, self.port)
            mail.login(self.user, self.password)
            mail.select(self.folder)

            # Search for recent emails
            since_date = (datetime.now() - timedelta(hours=self.since_hours)).strftime("%d-%b-%Y")
            _, msg_ids = mail.search(None, f'(SINCE {since_date})')

            if not msg_ids[0]:
                print(f"[EmailSource] No emails since {since_date} in {self.folder}")
                mail.logout()
                return []

            id_list = msg_ids[0].split()
            print(f"[EmailSource] Found {len(id_list)} email(s) since {since_date}")

            for mid in id_list:
                _, msg_data = mail.fetch(mid, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0]
                if isinstance(raw, tuple):
                    msg = email.message_from_bytes(raw[1])
                else:
                    continue

                subject = _decode_header(msg.get("Subject", ""))
                sender = _decode_header(msg.get("From", ""))
                body = _extract_body(msg)

                # Build content with email context
                content = f"From: {sender}\nSubject: {subject}\n\n{body}"

                doc = RawDoc(
                    content=content,
                    source_type=self.source_type,
                    source_label=f"Email: {subject}",
                    meta={
                        "sender": sender,
                        "subject": subject,
                        "message_id": msg.get("Message-ID", ""),
                    },
                    timestamp=parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None,
                )
                docs.append(doc)

                # Extract supported attachments
                attachments = _extract_attachments(msg, tmp_dir)
                for att_path in attachments:
                    try:
                        file_docs = FileSource(att_path).fetch()
                        for fd in file_docs:
                            fd.meta["parent_email"] = subject
                            fd.meta["sender"] = sender
                            docs.append(fd)
                    except Exception as e:
                        print(f"[EmailSource] Attachment {att_path.name} failed: {e}")

            mail.logout()
        except imaplib.IMAP4.error as e:
            print(f"[EmailSource] IMAP error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[EmailSource] Error: {e}", file=sys.stderr)

        print(f"[EmailSource] Fetched {len(docs)} document(s) (incl. attachments)")
        return docs


class StdinSource(BaseSource):
    """Read document from stdin (paste mode / pipe)."""

    source_type = "stdin"

    def __init__(self, label: str = "stdin"):
        self.label = label

    def fetch(self) -> List[RawDoc]:
        import sys
        content = sys.stdin.read()
        if not content.strip():
            return []
        return [RawDoc(
            content=content,
            source_type=self.source_type,
            source_label=self.label,
        )]
