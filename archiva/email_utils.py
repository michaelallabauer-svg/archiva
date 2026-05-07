"""Email (.eml) parsing helpers for Archiva."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import escape
from pathlib import Path
from typing import Iterable


@dataclass(slots=True)
class EmailAttachmentInfo:
    filename: str
    content_type: str
    size_bytes: int
    content_id: str | None = None


@dataclass(slots=True)
class ParsedEmail:
    subject: str
    from_: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    date: str
    message_id: str
    plain_body: str
    html_body_text: str
    attachments: list[EmailAttachmentInfo]

    @property
    def body_text(self) -> str:
        return self.plain_body.strip() or self.html_body_text.strip()

    def fulltext(self) -> str:
        parts = [
            self.subject,
            self.from_,
            " ".join(self.to),
            " ".join(self.cc),
            self.date,
            self.message_id,
            self.body_text,
            " ".join(attachment.filename for attachment in self.attachments),
        ]
        return "\n".join(part for part in parts if part).strip()


def parse_eml(path: Path) -> ParsedEmail:
    with path.open("rb") as fh:
        message = BytesParser(policy=policy.default).parse(fh)
    return parsed_email_from_message(message)


def parsed_email_from_message(message: Message) -> ParsedEmail:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[EmailAttachmentInfo] = []

    for part in message.walk() if message.is_multipart() else [message]:
        content_disposition = (part.get_content_disposition() or "").lower()
        content_type = (part.get_content_type() or "application/octet-stream").lower()
        filename = part.get_filename()

        if content_disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                EmailAttachmentInfo(
                    filename=filename or "unnamed-attachment",
                    content_type=content_type,
                    size_bytes=len(payload),
                    content_id=part.get("Content-ID"),
                )
            )
            continue

        if content_type == "text/plain":
            text = _part_text(part)
            if text.strip():
                plain_parts.append(text.strip())
        elif content_type == "text/html":
            text = _html_to_text(_part_text(part))
            if text.strip():
                html_parts.append(text.strip())

    date_value = ""
    if message.get("Date"):
        try:
            date_value = parsedate_to_datetime(message.get("Date", "")).isoformat()
        except Exception:
            date_value = str(message.get("Date", ""))

    return ParsedEmail(
        subject=str(message.get("Subject", "") or ""),
        from_=_format_addresses([message.get("From", "")]),
        to=_address_list([message.get("To", "")]),
        cc=_address_list([message.get("Cc", "")]),
        bcc=_address_list([message.get("Bcc", "")]),
        date=date_value,
        message_id=str(message.get("Message-ID", "") or ""),
        plain_body="\n\n".join(plain_parts),
        html_body_text="\n\n".join(html_parts),
        attachments=attachments,
    )


def render_email_preview_html(parsed: ParsedEmail, title: str) -> bytes:
    to_html = ", ".join(escape(item) for item in parsed.to) or "—"
    cc_html = ", ".join(escape(item) for item in parsed.cc) or "—"
    attachment_rows = "".join(
        "<tr>"
        f"<td>{escape(attachment.filename)}</td>"
        f"<td>{escape(attachment.content_type)}</td>"
        f"<td>{attachment.size_bytes}</td>"
        "</tr>"
        for attachment in parsed.attachments
    ) or '<tr><td colspan="3">Keine Anhänge</td></tr>'
    body = escape(parsed.body_text or "Kein Textbody gefunden.")
    html = f"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background:#0b1020; color:#eef2ff; }}
    .page {{ padding:20px; }}
    .card {{ border-radius:18px; background:#121933; border:1px solid #2d3b69; overflow:hidden; margin-bottom:16px; }}
    .header, .body {{ padding:18px 20px; }}
    .header {{ border-bottom:1px solid rgba(255,255,255,.06); background:#11182f; }}
    h1 {{ margin:0; font-size:1.15rem; }}
    .meta {{ display:grid; grid-template-columns:140px 1fr; gap:10px; padding:8px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
    .key {{ color:#a8b2d1; font-weight:700; }}
    pre {{ white-space:pre-wrap; word-break:break-word; font: .95rem/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ border:1px solid rgba(255,255,255,.08); padding:10px; text-align:left; }}
    th {{ background:#0f1630; }}
  </style>
</head>
<body>
  <div class="page">
    <div class="card">
      <div class="header"><h1>{escape(parsed.subject or title)}</h1></div>
      <div class="body">
        <div class="meta"><div class="key">Von</div><div>{escape(parsed.from_ or '—')}</div></div>
        <div class="meta"><div class="key">An</div><div>{to_html}</div></div>
        <div class="meta"><div class="key">CC</div><div>{cc_html}</div></div>
        <div class="meta"><div class="key">Datum</div><div>{escape(parsed.date or '—')}</div></div>
        <div class="meta"><div class="key">Message-ID</div><div>{escape(parsed.message_id or '—')}</div></div>
      </div>
    </div>
    <div class="card"><div class="header"><h1>Nachricht</h1></div><div class="body"><pre>{body}</pre></div></div>
    <div class="card"><div class="header"><h1>Anhänge</h1></div><div class="body"><table><thead><tr><th>Dateiname</th><th>Typ</th><th>Bytes</th></tr></thead><tbody>{attachment_rows}</tbody></table></div></div>
  </div>
</body>
</html>
"""
    return html.encode("utf-8")


def email_metadata(parsed: ParsedEmail) -> dict[str, object]:
    """Return stable metadata for storing mail headers and future child attachments."""
    return {
        "subject": parsed.subject,
        "from": parsed.from_,
        "to": parsed.to,
        "cc": parsed.cc,
        "bcc": parsed.bcc,
        "date": parsed.date,
        "message_id": parsed.message_id,
        "attachment_count": len(parsed.attachments),
        "attachments": [
            {
                "index": index,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size_bytes": attachment.size_bytes,
                "content_id": attachment.content_id,
                "child_document_id": None,
                "child_status": "pending",
            }
            for index, attachment in enumerate(parsed.attachments)
        ],
    }


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else str(content or "")
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def _address_list(values: Iterable[str | None]) -> list[str]:
    result = []
    for name, address in getaddresses([value or "" for value in values]):
        if name and address:
            result.append(f"{name} <{address}>")
        elif address:
            result.append(address)
        elif name:
            result.append(name)
    return result


def _format_addresses(values: Iterable[str | None]) -> str:
    return ", ".join(_address_list(values))


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()
