import mimetypes
import secrets
import string
import time
import quopri
import base64
from email.utils import formatdate, formataddr, encode_rfc2231
from email.header import Header
from typing import Optional, Tuple

_CRLF_RE_CHARS = str.maketrans("", "", "\r\n")
_BASE36 = string.ascii_uppercase + string.digits


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
    def _generate_queue_id() -> str:
        return "".join(secrets.choice(_BASE36) for _ in range(10))

    @classmethod
    def generate_message_id(cls, sender_domain: str) -> tuple:
        if not sender_domain or "." not in sender_domain:
            raise ValueError(
                f"Invalid sender domain for Message-ID: {sender_domain!r}"
            )
        now = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        queue_id = cls._generate_queue_id()
        return f"<{now}.{queue_id}@{sender_domain}>", queue_id

    @staticmethod
    def generate_boundary() -> str:
        ts = int(time.time() * 1000)
        rand_hex = secrets.token_hex(8)
        return f"----=_Part_{ts}_{rand_hex}"

    @staticmethod
    def _is_ascii(text: str) -> bool:
        try:
            text.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    @classmethod
    def _encode_body(cls, text: str) -> tuple:
        if cls._is_ascii(text):
            return "7bit", text
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

        message_id, queue_id = cls.generate_message_id(sender_domain)
        date_str = formatdate(usegmt=True)
        from_header = formataddr((from_name, from_email))
        subject_encoded = cls._encode_header_value(subject)

        received = (
            f"from localhost (localhost [127.0.0.1])\r\n"
            f"\tby {sender_domain} (Postfix) with ESMTP id {queue_id}\r\n"
            f"\tfor <{to_email}>; {date_str}"
        )

        if attachment:
            return cls._build_mixed(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, attachment,
                received,
            )
        return cls._build_alternative(
            date_str, from_header, to_email, message_id,
            subject_encoded, html_body, plain_body, received,
        )

    @classmethod
    def _header_block(
        cls, date_str: str, from_header: str, to_email: str,
        message_id: str, subject: str, content_type: str,
        received: str,
    ) -> list:
        lines = [
            f"Received: {received}",
            f"Date: {date_str}",
            cls._fold_header("From", from_header),
            f"To: {to_email}",
            f"Message-ID: {message_id}",
            cls._fold_header("Subject", subject),
            "Auto-Submitted: auto-generated",
            "MIME-Version: 1.0",
            f"Content-Type: {content_type}",
        ]
        return lines

    @classmethod
    def _build_alternative(
        cls, date_str: str, from_header: str, to_email: str,
        message_id: str, subject: str, html_body: str, plain_body: str,
        received: str,
    ) -> str:
        boundary = cls.generate_boundary()
        headers = cls._header_block(
            date_str, from_header, to_email, message_id, subject,
            f'multipart/alternative; boundary="{boundary}"',
            received,
        )
        plain_enc, plain_data = cls._encode_body(plain_body)
        html_enc, html_data = cls._encode_body(html_body)

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
        attachment: Tuple[str, bytes], received: str,
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

        plain_enc, plain_data = cls._encode_body(plain_body)
        html_enc, html_data = cls._encode_body(html_body)

        headers = cls._header_block(
            date_str, from_header, to_email, message_id, subject,
            f'multipart/mixed; boundary="{mixed_boundary}"',
            received,
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
