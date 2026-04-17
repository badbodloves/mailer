import os
import random
import time
from email.utils import formatdate, formataddr
from typing import Optional, Tuple


class MIMEBuilder:

    @staticmethod
    def generate_message_id(sender_domain: str) -> str:
        now = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        rand_hex = "%032x" % random.getrandbits(128)
        return f"<{now}.Z.{rand_hex}@{sender_domain}>"

    @staticmethod
    def generate_boundary() -> str:
        ts = int(time.time() * 1000)
        rand_hex = "%016x" % random.getrandbits(64)
        return f"----=_Part_{ts}_{rand_hex}"

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
        sender_domain = from_email.split("@")[1] if "@" in from_email else "localhost"
        message_id = cls.generate_message_id(sender_domain)
        date_str = formatdate(localtime=True)
        from_header = formataddr((from_name, from_email))

        if attachment:
            return cls._build_mixed(
                date_str, from_header, to_email, message_id,
                subject, html_body, plain_body, attachment,
            )
        return cls._build_alternative(
            date_str, from_header, to_email, message_id,
            subject, html_body, plain_body,
        )

    @classmethod
    def _build_alternative(
        cls, date_str: str, from_header: str, to_email: str,
        message_id: str, subject: str, html_body: str, plain_body: str,
    ) -> str:
        boundary = cls.generate_boundary()
        lines = [
            f"Date: {date_str}",
            f"From: {from_header}",
            f"To: {to_email}",
            f"Message-ID: {message_id}",
            f"Subject: {subject}",
            "MIME-Version: 1.0",
            f'Content-Type: multipart/alternative; boundary="{boundary}"',
            "",
            f"--{boundary}",
            "Content-Type: text/plain; charset=utf-8",
            "Content-Transfer-Encoding: quoted-printable",
            "",
            cls._encode_qp(plain_body),
            "",
            f"--{boundary}",
            "Content-Type: text/html; charset=utf-8",
            "Content-Transfer-Encoding: quoted-printable",
            "",
            cls._encode_qp(html_body),
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

        import base64
        att_b64 = base64.b64encode(att_data).decode("ascii")
        att_b64_lines = "\r\n".join(
            att_b64[i : i + 76] for i in range(0, len(att_b64), 76)
        )

        lines = [
            f"Date: {date_str}",
            f"From: {from_header}",
            f"To: {to_email}",
            f"Message-ID: {message_id}",
            f"Subject: {subject}",
            "MIME-Version: 1.0",
            f'Content-Type: multipart/mixed; boundary="{mixed_boundary}"',
            "",
            f"--{mixed_boundary}",
            f'Content-Type: multipart/alternative; boundary="{alt_boundary}"',
            "",
            f"--{alt_boundary}",
            "Content-Type: text/plain; charset=utf-8",
            "Content-Transfer-Encoding: quoted-printable",
            "",
            cls._encode_qp(plain_body),
            "",
            f"--{alt_boundary}",
            "Content-Type: text/html; charset=utf-8",
            "Content-Transfer-Encoding: quoted-printable",
            "",
            cls._encode_qp(html_body),
            "",
            f"--{alt_boundary}--",
            "",
            f"--{mixed_boundary}",
            f"Content-Type: application/octet-stream; name=\"{att_filename}\"",
            f"Content-Disposition: attachment; filename=\"{att_filename}\"",
            "Content-Transfer-Encoding: base64",
            "",
            att_b64_lines,
            "",
            f"--{mixed_boundary}--",
            "",
        ]
        return "\r\n".join(lines)

    @staticmethod
    def _encode_qp(text: str) -> str:
        import quopri
        encoded = quopri.encodestring(text.encode("utf-8"), quotetabs=True)
        return encoded.decode("ascii").replace("\n", "\r\n")
