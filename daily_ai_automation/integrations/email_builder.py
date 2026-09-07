"""MIME composition (PRD section 19).

Greetings use multipart/related with the image as an inline CID attachment.
That is the one arrangement that renders reliably everywhere: a remote <img src>
is blocked by default in most clients, and a plain attachment appears below the
message instead of inside it.
"""

from __future__ import annotations

import hashlib
from email.message import EmailMessage
from email.utils import make_msgid


def build_greeting_email(
    *,
    to_address: str,
    to_name: str,
    subject: str,
    greeting_line: str,
    body_paragraphs: list[str],
    closing_line: str,
    sender_name: str,
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
) -> EmailMessage:
    """Compose the greeting as a multipart/alternative + related message."""
    message = EmailMessage()
    message["To"] = f"{to_name} <{to_address}>" if to_name else to_address
    message["Subject"] = subject

    text_body = "\n\n".join(
        [greeting_line, *body_paragraphs, closing_line, sender_name]
    )
    message.set_content(text_body)

    cid = make_msgid(domain="greeting.local")
    image_html = (
        f'<p><img src="cid:{cid[1:-1]}" alt="" '
        'style="max-width:100%;height:auto;border-radius:8px;"></p>'
        if image_bytes
        else ""
    )
    paragraphs = "\n  ".join(f"<p>{_escape(p)}</p>" for p in body_paragraphs)

    html = f"""<html>
<body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;
             line-height:1.6;color:#1a1a1a;max-width:600px;">
  <p>{_escape(greeting_line)}</p>
  {image_html}
  {paragraphs}
  <p>{_escape(closing_line)}</p>
  <p>{_escape(sender_name)}</p>
</body>
</html>"""

    message.add_alternative(html, subtype="html")

    if image_bytes:
        # The image must attach to the HTML alternative, not the top-level
        # message, or clients that prefer text/plain will show it as a
        # dangling attachment.
        html_part = message.get_payload()[-1]
        maintype, _, subtype = image_mime.partition("/")
        html_part.add_related(
            image_bytes,
            maintype=maintype or "image",
            subtype=subtype or "png",
            cid=cid,
        )

    return message


def build_report_email(
    *, to_address: str, subject: str, html_body: str, text_body: str
) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def content_hash(*parts: str) -> str:
    """Stable hash of the generated content, stored on the send log row.

    Lets a future run tell "we already sent this" apart from "we sent something
    different", which matters if the greeting template is ever changed.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def _escape(text: str) -> str:
    import html

    return html.escape(text, quote=False)
