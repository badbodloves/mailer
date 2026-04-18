import mimetypes
import secrets
import time
import quopri
import base64
from email.utils import formatdate, formataddr, encode_rfc2231
from email.header import Header
from typing import Optional, Tuple, List

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
            return Header(value, "utf-8", maxlinelen=76).encode()

    @staticmethod
    def generate_message_id(sender_domain: str) -> str:
        if not sender_domain or "." not in sender_domain:
            raise ValueError(f"Invalid sender domain for Message-ID: {sender_domain!r}")
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
        inline_images: Optional[List[Tuple[bytes, str, str]]] = None,
    ) -> str:
        from_name = cls._sanitize(from_name)
        from_email = cls._sanitize(from_email)
        to_email = cls._sanitize(to_email)
        subject = cls._sanitize(subject)

        sender_domain = from_email.split("@")[1] if "@" in from_email else ""
        if not sender_domain or "." not in sender_domain:
            raise ValueError(f"Cannot extract valid domain from From address: {from_email!r}")
        if "@" not in to_email or "." not in to_email.split("@")[-1]:
            raise ValueError(f"Invalid To address: {to_email!r}")

        message_id = cls.generate_message_id(sender_domain)
        date_str = formatdate(usegmt=True)
        from_header = formataddr((cls._encode_header_value(from_name), from_email))
        subject_encoded = cls._encode_header_value(subject)

        has_inline = bool(inline_images)
        has_attach = bool(attachment)

        if has_attach and has_inline:
            return cls._build_mixed_related(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, attachment, inline_images,
            )
        if has_attach:
            return cls._build_mixed(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, attachment,
            )
        if has_inline:
            return cls._build_related(
                date_str, from_header, to_email, message_id,
                subject_encoded, html_body, plain_body, inline_images,
            )
        return cls._build_alternative(
            date_str, from_header, to_email, message_id,
            subject_encoded, html_body, plain_body,
        )

    @classmethod
    def _header_block(cls, date_str, from_header, to_email,
                      message_id, subject, content_type) -> list:
        return [
            f"Date: {date_str}",
            f"From: {from_header}",
            f"To: {to_email}",
            f"Message-ID: {message_id}",
            f"Subject: {subject}",
            "Auto-Submitted: auto-generated",
            "MIME-Version: 1.0",
            f"Content-Type: {content_type}",
        ]

    @classmethod
    def _alt_part(cls, boundary, html_body, plain_body) -> list:
        plain_enc, plain_data = cls._get_best_encoding(plain_body)
        html_enc, html_data = cls._get_best_encoding(html_body)
        return [
            f"--{boundary}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Transfer-Encoding: {plain_enc}",
            "", plain_data, "",
            f"--{boundary}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Transfer-Encoding: {html_enc}",
            "", html_data, "",
            f"--{boundary}--",
        ]

    @classmethod
    def _inline_parts(cls, inline_images) -> list:
        lines = []
        for img_data, cid, mime_type in inline_images:
            b64 = base64.b64encode(img_data).decode("ascii")
            b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
            ext = mime_type.split("/")[-1]
            lines += [
                f"Content-Type: {mime_type}; name=\"logo.{ext}\"",
                f"Content-Transfer-Encoding: base64",
                f"Content-ID: <{cid}>",
                f"Content-Disposition: inline; filename=\"logo.{ext}\"",
                "", b64_lines, "",
            ]
        return lines

    @classmethod
    def _attach_part(cls, att_filename, att_data) -> list:
        mime_type = mimetypes.guess_type(att_filename)[0] or "application/octet-stream"
        try:
            att_filename.encode("ascii")
            name_p = f'name="{att_filename}"'
            disp_p = f'filename="{att_filename}"'
        except UnicodeEncodeError:
            enc = encode_rfc2231(att_filename, "utf-8")
            name_p = f"name*={enc}"
            disp_p = f"filename*={enc}"
        b64 = base64.b64encode(att_data).decode("ascii")
        b64_lines = "\r\n".join(b64[i:i+76] for i in range(0, len(b64), 76))
        return [
            f"Content-Type: {mime_type}; {name_p}",
            f"Content-Disposition: attachment; {disp_p}",
            "Content-Transfer-Encoding: base64",
            "", b64_lines, "",
        ]

    @classmethod
    def _build_alternative(cls, date_str, from_header, to_email,
                           message_id, subject, html_body, plain_body) -> str:
        bnd = cls.generate_boundary()
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/alternative; boundary="{bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines += cls._alt_part(bnd, html_body, plain_body)
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_related(cls, date_str, from_header, to_email,
                       message_id, subject, html_body, plain_body, inline_images) -> str:
        rel_bnd = cls.generate_boundary()
        alt_bnd = cls.generate_boundary()
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/related; boundary="{rel_bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{rel_bnd}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt_bnd}"')
        lines.append("")
        lines += cls._alt_part(alt_bnd, html_body, plain_body)
        lines.append("")
        for img_data, cid, mime_type in inline_images:
            lines.append(f"--{rel_bnd}")
            lines += cls._inline_parts([(img_data, cid, mime_type)])
        lines.append(f"--{rel_bnd}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed(cls, date_str, from_header, to_email,
                     message_id, subject, html_body, plain_body, attachment) -> str:
        mix_bnd = cls.generate_boundary()
        alt_bnd = cls.generate_boundary()
        att_fn, att_data = attachment
        att_fn = cls._sanitize(att_fn)
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/mixed; boundary="{mix_bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix_bnd}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt_bnd}"')
        lines.append("")
        lines += cls._alt_part(alt_bnd, html_body, plain_body)
        lines.append("")
        lines.append(f"--{mix_bnd}")
        lines += cls._attach_part(att_fn, att_data)
        lines.append(f"--{mix_bnd}--")
        lines.append("")
        return "\r\n".join(lines)

    @classmethod
    def _build_mixed_related(cls, date_str, from_header, to_email,
                             message_id, subject, html_body, plain_body,
                             attachment, inline_images) -> str:
        mix_bnd = cls.generate_boundary()
        rel_bnd = cls.generate_boundary()
        alt_bnd = cls.generate_boundary()
        att_fn, att_data = attachment
        att_fn = cls._sanitize(att_fn)
        headers = cls._header_block(date_str, from_header, to_email, message_id,
                                     subject, f'multipart/mixed; boundary="{mix_bnd}"')
        lines = headers + ["", "This is a multi-part message in MIME format.", ""]
        lines.append(f"--{mix_bnd}")
        lines.append(f'Content-Type: multipart/related; boundary="{rel_bnd}"')
        lines.append("")
        lines.append(f"--{rel_bnd}")
        lines.append(f'Content-Type: multipart/alternative; boundary="{alt_bnd}"')
        lines.append("")
        lines += cls._alt_part(alt_bnd, html_body, plain_body)
        lines.append("")
        for img_data, cid, mime_type in inline_images:
            lines.append(f"--{rel_bnd}")
            lines += cls._inline_parts([(img_data, cid, mime_type)])
        lines.append(f"--{rel_bnd}--")
        lines.append("")
        lines.append(f"--{mix_bnd}")
        lines += cls._attach_part(att_fn, att_data)
        lines.append(f"--{mix_bnd}--")
        lines.append("")
        return "\r\n".join(lines)
