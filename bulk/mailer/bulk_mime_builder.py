"""Bulk MIME Builder.

Builds RFC-compliant newsletter/bulk emails with proper List-* headers,
Precedence, Feedback-ID, VERP envelope, and X-Entity-Ref-ID.

NO Anti-Fingerprint. NO Spintax obfuscation.
This is meant to look like legitimate ESP output.
"""
import re
import mimetypes
import secrets
import time
import quopri
import base64
import uuid
from email.utils import formataddr, encode_rfc2231
from email.header import Header
from typing import Optional, Tuple, List, Dict

_CRLF = str.maketrans("", "", "\r\n")
_LINE_LENGTH_RE = re.compile(r"[^\r\n]{997,}")
_FEEDBACK_RE = re.compile(r"^[\w\-.:]*:[\w\-.:]*:[\w\-.:]*:[\w\-]{5,15}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_LIST_ID_RE = re.compile(r"^[\w\-]+(\.[\w\-]+)+$")
_RESERVED_HEADERS = frozenset({
    "from", "to", "subject", "date", "message-id", "mime-version",
    "content-type", "list-unsubscribe", "list-unsubscribe-post",
    "list-id", "precedence", "feedback-id", "x-entity-ref-id", "reply-to",
})


class BulkMIMEBuilder:

    @staticmethod
    def _sanitize(v: str) -> str:
        return v.translate(_CRLF).strip()

    @staticmethod
    def _encode_header(v: str) -> str:
        try:
            v.encode("ascii")
            return v
        except UnicodeEncodeError:
            return Header(v, "utf-8", maxlinelen=76).encode()

    @staticmethod
    def _get_encoding(text: str, force_qp: bool = False) -> tuple:
        if not force_qp:
            try:
                text.encode("ascii")
                if not _LINE_LENGTH_RE.search(text):
                    return "7bit", text
            except UnicodeEncodeError:
                pass
        enc = quopri.encodestring(text.encode("utf-8"), quotetabs=True)
        return "quoted-printable", enc.decode("ascii").replace("\n", "\r\n")

    @staticmethod
    def _boundary() -> str:
        return f"----=_Part_{int(time.time()*1000)}_{secrets.token_hex(8)}"

    @staticmethod
    def _message_id(domain: str) -> str:
        ts = format(int(time.time() * 1000), "x")
        rnd = secrets.token_hex(16)
        return f"<{ts}.{rnd}@{domain}>"

    @staticmethod
    def _validate_email(email: str, label: str):
        if not _EMAIL_RE.match(email):
            raise ValueError(f"Invalid {label}: {email!r}")

    @classmethod
    def build_email(
        cls,
        from_name: str,
        from_email: str,
        reply_to_name: str,
        reply_to_email: str,
        to_email: str,
        subject: str,
        html_body: str,
        plain_body: str,
        list_id_token: str,
        list_id_name: str = "",
        unsubscribe_url: str = "",
        unsubscribe_mailto: str = "",
        feedback_id: str = "",
        bounce_domain: str = "",
        message_id_domain: str = "",
        recipient_id: str = "",
        attachment: Optional[Tuple[str, bytes]] = None,
        inline_images: Optional[List[Tuple[bytes, str, str]]] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        provider_type: str = "generic",
    ) -> Tuple[str, str, str]:
        """Build a bulk/newsletter email.

        Does NOT include a Date header — the send layer must prepend
        Date: at SMTP handoff time to avoid stale timestamps in queued batches.

        Returns (raw_message, envelope_from, verp_tag).
        """
        from_name = cls._sanitize(from_name)
        from_email = cls._sanitize(from_email)
        reply_to_name = cls._sanitize(reply_to_name)
        reply_to_email = cls._sanitize(reply_to_email)
        to_email = cls._sanitize(to_email)
        subject = cls._sanitize(subject)
        list_id_token = cls._sanitize(list_id_token)
        list_id_name = cls._sanitize(list_id_name)
        unsubscribe_url = cls._sanitize(unsubscribe_url)
        unsubscribe_mailto = cls._sanitize(unsubscribe_mailto)
        feedback_id = cls._sanitize(feedback_id)

        cls._validate_email(from_email, "From")
        cls._validate_email(to_email, "To")
        if reply_to_email:
            cls._validate_email(reply_to_email, "Reply-To")

        domain = from_email.split("@")[1]
        mid_domain = message_id_domain or domain

        if unsubscribe_url and not unsubscribe_url.startswith("https://"):
            raise ValueError("Unsubscribe URL must be HTTPS")

        if feedback_id and not _FEEDBACK_RE.match(feedback_id):
            raise ValueError(f"Invalid Feedback-ID format: {feedback_id!r}")

        if not _LIST_ID_RE.match(list_id_token):
            raise ValueError(f"Invalid list_id_token (use domain-style, no @ or spaces): {list_id_token!r}")

        safe_name = list_id_name.replace('"', '\\"') if list_id_name else ""
        list_id_str = f'"{safe_name}" <{list_id_token}>' if safe_name else f"<{list_id_token}>"

        msg_id = cls._message_id(mid_domain)
        from_header = formataddr((cls._encode_header(from_name), from_email))
        subject_enc = cls._encode_header(subject)
        entity_ref = str(uuid.uuid4())

        verp_tag = recipient_id or secrets.token_hex(8)
        b_domain = bounce_domain or f"bounce.{domain}"
        envelope_from = f"bounce+{verp_tag}@{b_domain}"

        is_ses = provider_type.lower() in ("ses", "aws", "amazon")

        headers = [f"From: {from_header}"]
        if reply_to_email:
            reply_header = formataddr((cls._encode_header(reply_to_name), reply_to_email))
            headers.append(f"Reply-To: {reply_header}")

        if not is_ses:
            headers.append(f"Return-Path: <{envelope_from}>")

        headers += [
            f"To: {to_email}",
            f"Subject: {subject_enc}",
            f"Message-ID: {msg_id}",
            f"List-Id: {list_id_str}",
        ]

        if unsubscribe_url and unsubscribe_mailto:
            headers.append(f"List-Unsubscribe: <{unsubscribe_url}>, <mailto:{unsubscribe_mailto}>")
            headers.append("List-Unsubscribe-Post: List-Unsubscribe=One-Click")

        headers.append("Precedence: bulk")

        if is_ses:
            headers.append(f"X-SES-MESSAGE-TAGS: campaign={verp_tag}")

        if feedback_id:
            headers.append(f"Feedback-ID: {feedback_id}")

        headers.append(f"X-Entity-Ref-ID: {entity_ref}")

        if custom_headers:
            for k, v in custom_headers.items():
                if k.lower() in _RESERVED_HEADERS:
                    raise ValueError(f"Cannot override reserved header: {k}")
                headers.append(f"{cls._sanitize(k)}: {cls._sanitize(v)}")

        headers.append("MIME-Version: 1.0")

        has_inline = bool(inline_images)
        has_attach = bool(attachment)

        if has_attach and has_inline:
            body = cls._build_mixed_related(headers, html_body, plain_body, attachment, inline_images)
        elif has_attach:
            body = cls._build_mixed(headers, html_body, plain_body, attachment)
        elif has_inline:
            body = cls._build_related(headers, html_body, plain_body, inline_images)
        else:
            body = cls._build_alternative(headers, html_body, plain_body)

        return body, envelope_from, verp_tag

    @classmethod
    def _alt_part(cls, bnd: str, html: str, plain: str) -> list:
        plain_enc, plain_data = cls._get_encoding(plain)
        html_enc, html_data = cls._get_encoding(html, force_qp=True)
        return [
            f"--{bnd}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "", plain_data, "",
            f"--{bnd}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "", html_data, "",
            f"--{bnd}--",
        ]

    @classmethod
    def _inline_parts(cls, images: List[Tuple[bytes, str, str]]) -> list:
        lines = []
        for data, cid, mime_type in images:
            b64 = base64.b64encode(data).decode("ascii")
            b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
            lines += [
                f"Content-Type: {mime_type}",
                "Content-Transfer-Encoding: base64",
                f"Content-ID: <{cid}>",
                "Content-Disposition: inline",
                "", b64_lines, "",
            ]
        return lines

    @classmethod
    def _attach_part(cls, filename: str, data: bytes) -> list:
        filename = cls._sanitize(filename)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            filename.encode("ascii")
            name_p = f'name="{filename}"'
            disp_p = f'filename="{filename}"'
        except UnicodeEncodeError:
            enc = encode_rfc2231(filename, "utf-8")
            name_p = f"name*={enc}"
            disp_p = f"filename*={enc}"
        b64 = base64.b64encode(data).decode("ascii")
        b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
        return [
            f"Content-Type: {mime_type}; {name_p}",
            f"Content-Disposition: attachment; {disp_p}",
            "Content-Transfer-Encoding: base64",
            "", b64_lines, "",
        ]

    @classmethod
    def _build_alternative(cls, headers: list, html: str, plain: str) -> str:
        h = list(headers)
        bnd = cls._boundary()
        h.append(f'Content-Type: multipart/alternative; boundary="{bnd}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""] + cls._alt_part(bnd, html, plain) + [""]
        return "\r\n".join(lines)

    @classmethod
    def _build_related(cls, headers: list, html: str, plain: str,
                       images: list) -> str:
        h = list(headers)
        rel = cls._boundary()
        alt = cls._boundary()
        h.append(f'Content-Type: multipart/related; boundary="{rel}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{rel}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt}"')
        lines.append("")
        lines += cls._alt_part(alt, html, plain)
        lines.append("")
        for data, cid, mime in images:
            lines.append(f"--{rel}")
            lines += cls._inline_parts([(data, cid, mime)])
        lines.append(f"--{rel}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed(cls, headers: list, html: str, plain: str,
                     attachment: Tuple[str, bytes]) -> str:
        h = list(headers)
        mix = cls._boundary()
        alt = cls._boundary()
        h.append(f'Content-Type: multipart/mixed; boundary="{mix}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt}"')
        lines.append("")
        lines += cls._alt_part(alt, html, plain)
        lines.append("")
        lines.append(f"--{mix}")
        lines += cls._attach_part(attachment[0], attachment[1])
        lines.append(f"--{mix}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed_related(cls, headers: list, html: str, plain: str,
                              attachment: Tuple[str, bytes],
                              images: list) -> str:
        h = list(headers)
        mix = cls._boundary()
        rel = cls._boundary()
        alt = cls._boundary()
        h.append(f'Content-Type: multipart/mixed; boundary="{mix}"')
        lines = h + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix}")
        lines.append(f'Content-Type: multipart/related; boundary="{rel}"')
        lines.append("")
        lines.append(f"--{rel}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt}"')
        lines.append("")
        lines += cls._alt_part(alt, html, plain)
        lines.append("")
        for data, cid, mime in images:
            lines.append(f"--{rel}")
            lines += cls._inline_parts([(data, cid, mime)])
        lines.append(f"--{rel}--")
        lines.append("")
        lines.append(f"--{mix}")
        lines += cls._attach_part(attachment[0], attachment[1])
        lines.append(f"--{mix}--")
        lines.append("")
        return "\r\n".join(lines)
