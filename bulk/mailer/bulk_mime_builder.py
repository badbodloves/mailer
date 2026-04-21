"""Bulk MIME Builder.

Builds RFC-compliant newsletter/bulk emails with proper List-* headers,
Precedence, Feedback-ID, VERP envelope, and X-Entity-Ref-ID.

NO Anti-Fingerprint. NO CID logos. NO Spintax obfuscation.
This is meant to look like legitimate ESP output (Mailchimp/SendGrid).
"""
import mimetypes
import secrets
import time
import quopri
import base64
import uuid
from email.utils import formatdate, formataddr, encode_rfc2231
from email.header import Header
from typing import Optional, Tuple, List

_CRLF = str.maketrans("", "", "\r\n")


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
            return Header(v, "utf-8", maxlinelen=998).encode()

    @staticmethod
    def _get_encoding(text: str) -> tuple:
        try:
            text.encode("ascii")
            return "7bit", text
        except UnicodeEncodeError:
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
    def _entity_ref_id() -> str:
        return str(uuid.uuid4())

    @classmethod
    def build_email(
        cls,
        from_name: str,
        from_email: str,
        reply_to: str,
        to_email: str,
        subject: str,
        html_body: str,
        plain_body: str,
        list_id: str,
        unsubscribe_url: str,
        unsubscribe_mailto: str,
        feedback_id: str = "",
        attachment: Optional[Tuple[str, bytes]] = None,
    ) -> Tuple[str, str]:
        """Build a bulk/newsletter email.

        Returns (raw_message, envelope_from).
        envelope_from is the VERP address for SMTP MAIL FROM.
        """
        from_name = cls._sanitize(from_name)
        from_email = cls._sanitize(from_email)
        to_email = cls._sanitize(to_email)
        subject = cls._sanitize(subject)

        domain = from_email.split("@")[1] if "@" in from_email else "localhost"
        msg_id = cls._message_id(domain)
        date_str = formatdate(localtime=True)
        from_header = formataddr((cls._encode_header(from_name), from_email))
        subject_enc = cls._encode_header(subject)
        entity_ref = cls._entity_ref_id()

        verp_tag = secrets.token_hex(8)
        envelope_from = f"bounce+{verp_tag}@bounce.{domain}"

        headers = [
            f"Date: {date_str}",
            f"From: {from_header}",
            f"Reply-To: <{cls._sanitize(reply_to)}>",
            f"To: {to_email}",
            f"Subject: {subject_enc}",
            f"Message-ID: {msg_id}",
            f"List-Id: {list_id}",
            f"List-Unsubscribe: <{unsubscribe_url}>, <mailto:{unsubscribe_mailto}>",
            "List-Unsubscribe-Post: List-Unsubscribe=One-Click",
            "Precedence: bulk",
        ]

        if feedback_id:
            headers.append(f"Feedback-ID: {feedback_id}")

        headers.append(f"X-Entity-Ref-ID: {entity_ref}")
        headers.append("MIME-Version: 1.0")

        if attachment:
            raw = cls._build_mixed(headers, html_body, plain_body, attachment)
        else:
            raw = cls._build_alternative(headers, html_body, plain_body)

        return raw, envelope_from

    @classmethod
    def _build_alternative(cls, headers: list, html: str, plain: str) -> str:
        bnd = cls._boundary()
        headers.append(f'Content-Type: multipart/alternative; boundary="{bnd}"')
        plain_enc, plain_data = cls._get_encoding(plain)
        html_enc, html_data = cls._get_encoding(html)
        lines = headers + [
            "",
            "This is a multi-part message in MIME format.",
            "",
            f"--{bnd}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "", plain_data, "",
            f"--{bnd}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "", html_data, "",
            f"--{bnd}--", "",
        ]
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed(cls, headers: list, html: str, plain: str,
                     attachment: Tuple[str, bytes]) -> str:
        mix = cls._boundary()
        alt = cls._boundary()
        att_fn, att_data = attachment
        att_fn = cls._sanitize(att_fn)
        mime_type = mimetypes.guess_type(att_fn)[0] or "application/octet-stream"

        try:
            att_fn.encode("ascii")
            name_p = f'name="{att_fn}"'
            disp_p = f'filename="{att_fn}"'
        except UnicodeEncodeError:
            enc = encode_rfc2231(att_fn, "utf-8")
            name_p = f"name*={enc}"
            disp_p = f"filename*={enc}"

        b64 = base64.b64encode(att_data).decode("ascii")
        b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
        plain_enc, plain_data = cls._get_encoding(plain)
        html_enc, html_data = cls._get_encoding(html)

        headers.append(f'Content-Type: multipart/mixed; boundary="{mix}"')
        lines = headers + [
            "",
            "This is a multi-part message in MIME format.",
            "",
            f"--{mix}",
            f'Content-Type: multipart/alternative; boundary="{alt}"',
            "",
            f"--{alt}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "", plain_data, "",
            f"--{alt}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "", html_data, "",
            f"--{alt}--", "",
            f"--{mix}",
            f"Content-Type: {mime_type}; {name_p}",
            f"Content-Disposition: attachment; {disp_p}",
            "Content-Transfer-Encoding: base64",
            "", b64_lines, "",
            f"--{mix}--", "",
        ]
        return "\r\n".join(lines)
