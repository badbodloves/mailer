import mimetypes
import secrets
import time
import quopri
import base64
from email.utils import formatdate, formataddr, encode_rfc2231
from email.header import Header
from typing import Optional, Tuple

_CRLF_RE_CHARS = str.maketrans("", "", "\r\n")


class MIMEBuilder:

    @staticmethod
    def _sanitize(value: str) -> str:
        return value.translate(_CRLF_RE_CHARS).strip()

    @staticmethod
    def _encode_header_value(value: str) -> str:
        try:
            value.encode("ascii")
            return value
        except UnicodeEncodeError:
            return Header(value, "utf-8").encode()

    @staticmethod
    def _fold_header(name: str, value: str) -> str:
        line = f"{name}: {value}"
        if len(line) <= 78:
            return line
        chunks = []
        while len(line) > 78:
            split = line.rfind(" ", 0, 78)
            if split <= len(name) + 2:
                split = 78
            chunks.append(line[:split])
            line = " " + line[split:].lstrip()
        chunks.append(line)
        return "\r\n".join(chunks)

    @staticmethod
    def generate_message_id(sender_domain: str) -> str:
        if not sender_domain or "." not in sender_domain:
            raise ValueError(
                f"Invalid sender domain for Message-ID: {sender_domain!r}"
            )
        ts_hex = format(int(time.time() * 1000), "x")
        rand_hex = secrets.token_hex(16)
        return f"<{ts_hex}.{rand_hex}@{sender_domain}>"

    @staticmethod
    def generate_boundary() -> str:
        ts = int(time.time() * 1000)
        rand_hex = secrets.token_hex(8)
        return f"----=_Part_{ts}_{rand_hex}"

    @staticmethod
    def _get_best_encoding(text: str) -> tuple:
        try:
            text.encode("ascii")
            return "7bit", text
        except UnicodeEncodeError:
            encoded = quopri.encodestring(text.encode("utf-8"), quotetabs=True)
            return "quoted-printable", encoded.decode("ascii").replace("\n", "\r\n")

    @classmethod
    def build_email(
        cls,
        from_name: str,
        from_email: str,
        to_email: str,
        subject: str,
        html_body: str,
        plain_body: str,
        attachment: Optional[Tuple[str, bytes]] = None,
    ) -> str:
        from_name = cls._sanitize(from_name)
        from_email = cls._sanitize(from_email)
        to_email = cls._sanitize(to_email)
        subject = cls._sanitize(subject)

        sender_domain = from_email.split("@")[1] if "@" in from_email else ""
        if not sender_domain or "." not in sender_domain:
            raise ValueError(
                f"Cannot extract valid domain from From address: {from_email!r}"
            )
        if "@" not in to_email or "." not in to_email.split("@")[-1]:
            raise ValueError(
                f"Invalid To address: {to_email!r}"
            )

        message_id = cls.generate_message_id(sender_domain)
        date_str = formatdate(usegmt=True)
        from_header = formataddr(
            (cls._encode_header_value(from_name), from_email)
        )
        subject_encoded = cls._encode_header_value(subject)

        if attachment:
            return cls._build_mixed(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, attachment,
            )
        return cls._build_alternative(
            date_str, from_header, to_email, message_id,
            subject_encoded, html_body, plain_body,
        )

    @classmethod
    def _header_block(
        cls, date_str: str, from_header: str, to_email: str,
        message_id: str, subject: str, content_type: str,
    ) -> list:
        return [
            f"Date: {date_str}",
            cls._fold_header("From", from_header),
            f"To: {to_email}",
            f"Message-ID: {message_id}",
            cls._fold_header("Subject", subject),
            "Auto-Submitted: auto-generated",
            "MIME-Version: 1.0",
            f"Content-Type: {content_type}",
        ]

    @classmethod
    def _build_alternative(
        cls, date_str: str, from_header: str, to_email: str,
        message_id: str, subject: str, html_body: str, plain_body: str,
    ) -> str:
        boundary = cls.generate_boundary()
        headers = cls._header_block(
            date_str, from_header, to_email, message_id, subject,
            f'multipart/alternative; boundary="{boundary}"',
        )
        plain_enc, plain_data = cls._get_best_encoding(plain_body)
        html_enc, html_data = cls._get_best_encoding(html_body)

        lines = headers + [
            "",
            "This is a multi-part message in MIME format.",
            "",
            f"--{boundary}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "",
            plain_data,
            "",
            f"--{boundary}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "",
            html_data,
            "",
            f"--{boundary}--",
            "",
        ]
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed(
        cls, date_str: str, from_header: str, to_email: str,
        message_id: str, subject: str, html_body: str, plain_body: str,
        attachment: Tuple[str, bytes],
    ) -> str:
        mixed_boundary = cls.generate_boundary()
        alt_boundary = cls.generate_boundary()
        att_filename, att_data = attachment
        att_filename = cls._sanitize(att_filename)

        mime_type = mimetypes.guess_type(att_filename)[0] or "application/octet-stream"

        try:
            att_filename.encode("ascii")
            name_param = f'name="{att_filename}"'
            disp_param = f'filename="{att_filename}"'
        except UnicodeEncodeError:
            encoded = encode_rfc2231(att_filename, "utf-8")
            name_param = f"name*={encoded}"
            disp_param = f"filename*={encoded}"

        att_b64 = base64.b64encode(att_data).decode("ascii")
        att_b64_lines = "\r\n".join(
            att_b64[i : i + 76] for i in range(0, len(att_b64), 76)
        )

        plain_enc, plain_data = cls._get_best_encoding(plain_body)
        html_enc, html_data = cls._get_best_encoding(html_body)

        headers = cls._header_block(
            date_str, from_header, to_email, message_id, subject,
            f'multipart/mixed; boundary="{mixed_boundary}"',
        )
        lines = headers + [
            "",
            "This is a multi-part message in MIME format.",
            "",
            f"--{mixed_boundary}",
            f'Content-Type: multipart/alternative; boundary="{alt_boundary}"',
            "",
            f"--{alt_boundary}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "",
            plain_data,
            "",
            f"--{alt_boundary}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "",
            html_data,
            "",
            f"--{alt_boundary}--",
            "",
            f"--{mixed_boundary}",
            f"Content-Type: {mime_type}; {name_param}",
            f"Content-Disposition: attachment; {disp_param}",
            "Content-Transfer-Encoding: base64",
            "",
            att_b64_lines,
            "",
            f"--{mixed_boundary}--",
            "",
        ]
        return "\r\n".join(lines)
