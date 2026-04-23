"""MIME Profile Rotation — alternative header structures that look like different ESPs.

Each profile modifies the raw MIME output from MIMEBuilder AFTER it's built.
The original builder stays untouched.
"""
import re
import secrets
import time
import random
from email.utils import formatdate

PROFILES = {
    "default": {
        "description": "Standard (current MIMEBuilder output)",
    },
    "outlook_style": {
        "description": "Microsoft Outlook / Exchange style",
        "boundary_prefix": "----=_NextPart_",
        "message_id_format": "outlook",
        "extra_headers": {
            "X-Mailer": "Microsoft Outlook 16.0",
            "X-MimeOLE": "Produced By Microsoft MimeOLE V16.4",
        },
        "remove_headers": ["Auto-Submitted"],
    },
    "thunderbird_style": {
        "description": "Mozilla Thunderbird style",
        "boundary_prefix": "------------",
        "message_id_format": "thunderbird",
        "extra_headers": {
            "User-Agent": "Mozilla Thunderbird 115.0",
        },
        "remove_headers": ["Auto-Submitted"],
    },
    "apple_mail": {
        "description": "Apple Mail style",
        "boundary_prefix": "Apple-Mail=_",
        "message_id_format": "apple",
        "extra_headers": {
            "X-Mailer": "Apple Mail",
            "X-Universally-Unique-Identifier": lambda: str(__import__('uuid').uuid4()).upper(),
        },
        "remove_headers": ["Auto-Submitted"],
    },
    "generic_webmail": {
        "description": "Generic webmail (Gmail/Yahoo-like)",
        "boundary_prefix": "000000000000",
        "message_id_format": "webmail",
        "remove_headers": ["Auto-Submitted"],
    },
    "sendgrid_style": {
        "description": "SendGrid ESP style",
        "extra_headers": {
            "X-SG-EID": lambda: secrets.token_hex(16),
        },
        "remove_headers": ["Auto-Submitted"],
        "header_order": ["Date", "From", "To", "Subject", "MIME-Version",
                          "Content-Type", "Message-ID"],
    },
    "mailchimp_style": {
        "description": "Mailchimp ESP style",
        "extra_headers": {
            "X-MC-User": lambda: secrets.token_hex(6),
            "X-Mailer": "MailChimp Mailer",
            "Precedence": "bulk",
        },
        "remove_headers": ["Auto-Submitted"],
    },
}


def _generate_message_id(domain: str, style: str = "default") -> str:
    if style == "outlook":
        p1 = secrets.token_hex(4)
        p2 = secrets.token_hex(4)
        p3 = secrets.token_hex(4)
        return f"<{p1}{p2}{p3}@{domain}>"
    elif style == "thunderbird":
        uid = secrets.token_hex(18)
        return f"<{uid}@{domain}>"
    elif style == "apple":
        uid = str(__import__('uuid').uuid4()).upper()
        return f"<{uid}@{domain}>"
    elif style == "webmail":
        rand = secrets.token_urlsafe(24)
        return f"<{rand}@mail.{domain}>"
    else:
        ts = format(int(time.time() * 1000), "x")
        rand = secrets.token_hex(16)
        return f"<{ts}.{rand}@{domain}>"


def _generate_boundary(style: str = "default") -> str:
    prefix = PROFILES.get(style, {}).get("boundary_prefix", "----=_Part_")
    if style == "outlook_style":
        return f"{prefix}{secrets.token_hex(3)}_{int(time.time()):08x}_{secrets.token_hex(4)}"
    elif style == "thunderbird_style":
        return f"{prefix}{secrets.token_hex(12)}"
    elif style == "apple_mail":
        return f"{prefix}{secrets.token_hex(8).upper()}"
    elif style == "generic_webmail":
        return f"{prefix}{secrets.token_hex(14)}"
    else:
        return f"----=_Part_{int(time.time()*1000)}_{secrets.token_hex(8)}"


def apply_profile(raw_mime: str, profile_name: str, from_email: str = "") -> str:
    """Apply a MIME profile to an already-built raw message.
    Modifies headers, boundary format, message-id style."""
    if profile_name == "default" or profile_name not in PROFILES:
        return raw_mime

    profile = PROFILES[profile_name]
    domain = from_email.split("@")[1] if "@" in from_email else "mail.local"

    # Split headers from body
    if "\r\n\r\n" in raw_mime:
        header_part, body = raw_mime.split("\r\n\r\n", 1)
    else:
        return raw_mime

    headers = header_part.split("\r\n")

    # Remove specified headers
    remove = set(h.lower() for h in profile.get("remove_headers", []))
    headers = [h for h in headers if h.split(":")[0].lower() not in remove]

    # Replace Message-ID
    mid_format = profile.get("message_id_format", "default")
    new_mid = _generate_message_id(domain, mid_format)
    headers = [re.sub(r"^Message-ID:.*", f"Message-ID: {new_mid}", h)
               if h.startswith("Message-ID:") else h for h in headers]

    # Replace boundary in Content-Type and body
    old_boundaries = re.findall(r'boundary="([^"]+)"', header_part)
    boundary_map = {}
    for old_b in old_boundaries:
        new_b = _generate_boundary(profile_name)
        boundary_map[old_b] = new_b

    # Add extra headers before MIME-Version
    extra = profile.get("extra_headers", {})
    extra_lines = []
    for k, v in extra.items():
        val = v() if callable(v) else v
        extra_lines.append(f"{k}: {val}")

    # Insert extra headers before MIME-Version line
    new_headers = []
    for h in headers:
        if h.startswith("MIME-Version:") and extra_lines:
            new_headers.extend(extra_lines)
            extra_lines = []
        new_headers.append(h)
    if extra_lines:
        new_headers.extend(extra_lines)

    # Apply boundary replacements
    result_header = "\r\n".join(new_headers)
    result_body = body
    for old_b, new_b in boundary_map.items():
        result_header = result_header.replace(old_b, new_b)
        result_body = result_body.replace(old_b, new_b)

    return result_header + "\r\n\r\n" + result_body


def get_random_profile() -> str:
    """Pick a random profile name for rotation."""
    return random.choice(list(PROFILES.keys()))


def get_profile_names() -> list:
    """Return list of (name, description) tuples."""
    return [(k, v.get("description", k)) for k, v in PROFILES.items()]
