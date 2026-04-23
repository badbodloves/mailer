"""MIME Profile Rotation — verified header structures from real mail clients.

Each profile modifies the raw MIME output from MIMEBuilder AFTER it's built.
The original builder stays untouched.
Profiles verified against real-world email headers (April 2026).
"""
import re
import secrets
import time
import random
import uuid

PROFILES = {
    "default": {
        "description": "Standard (current MIMEBuilder output)",
    },
    "outlook_desktop": {
        "description": "Microsoft Outlook 16.0 Desktop (verified)",
        # Real: boundary="----=_NextPart_000_00BE_01DCD341.8D93F050"
        # Real: Message-ID: <00bd01dcd330$ca0b2050$5e2160f0$@domain>
        # Real: X-Mailer: Microsoft Outlook 16.0
        # Real: Thread-Index: AdzTMJZV0j8LCuh/QUaqhjXjejUR6A==
        # Real: Content-Language: de
        # Real headers: From, To, Subject, Date, Message-ID, MIME-Version,
        #   Content-Type, X-Mailer, Thread-Index, Content-Language
        # NO Auto-Submitted
    },
    "apple_mail_mac": {
        "description": "Apple Mail macOS (verified)",
        # Real: boundary=Apple-Mail-DB7D09F3-1DC5-4134-809D-914108EF7330
        # Real: Message-Id: <3BDFFB73-2E8B-46A1-9E7B-5390E41C7B6E@domain>
        # Real: Mime-Version: 1.0 (1.0)
        # Real: Content-Transfer-Encoding: 7bit (at top level!)
        # Real header ORDER: Content-Type, Content-Transfer-Encoding, From,
        #   Mime-Version, Subject, Message-Id, Date, To
        # NO Auto-Submitted, NO X-Mailer on Mac
    },
    "apple_mail_iphone": {
        "description": "iPhone Mail (verified)",
        # Real: boundary=Apple-Mail-9B2D474E-2C0E-4EB9-950E-C108D58C8AD8
        # Real: Message-Id: <1B4C84AB-D584-4D25-8D3B-1856B57AD907@domain>
        # Real: X-Mailer: iPhone Mail (22G100)
        # Real: Mime-Version: 1.0 (1.0)
        # Same boundary format as Mac, but has X-Mailer
    },
    "thunderbird": {
        "description": "Mozilla Thunderbird (verified)",
        # Real: Message-ID: <2fa39d13-9935-4c45-9bd3-add0b5482a27@domain>
        # Real: User-Agent: Mozilla Thunderbird
        # Real header ORDER: Message-ID, Date, MIME-Version, User-Agent, To, From, Subject, Content-Type
        # NO Auto-Submitted
    },
}


def _gen_outlook_message_id(domain: str) -> str:
    # Real format: <00bd01dcd330$ca0b2050$5e2160f0$@domain>
    p1 = secrets.token_hex(4)
    p2 = secrets.token_hex(4)
    p3 = secrets.token_hex(4)
    return f"<{p1}${p2}${p3}$@{domain}>"


def _gen_outlook_boundary() -> str:
    # Real: ----=_NextPart_000_00BE_01DCD341.8D93F050
    seq1 = f"{random.randint(0,999):03X}"
    seq2 = f"{random.randint(0,65535):04X}"
    ts = f"{random.randint(0x01000000, 0xFFFFFFFF):08X}"
    rand = f"{random.randint(0x10000000, 0xFFFFFFFF):08X}"
    return f"----=_NextPart_{seq1}_{seq2}_{ts}.{rand}"


def _gen_outlook_thread_index() -> str:
    import base64
    raw = secrets.token_bytes(16)
    return base64.b64encode(raw).decode().rstrip("=") + "=="


def _gen_apple_boundary() -> str:
    # Real: Apple-Mail-DB7D09F3-1DC5-4134-809D-914108EF7330
    return f"Apple-Mail-{str(uuid.uuid4()).upper()}"


def _gen_apple_message_id(domain: str) -> str:
    # Real: <3BDFFB73-2E8B-46A1-9E7B-5390E41C7B6E@domain>
    return f"<{str(uuid.uuid4()).upper()}@{domain}>"


def _gen_thunderbird_message_id(domain: str) -> str:
    # Real: <2fa39d13-9935-4c45-9bd3-add0b5482a27@domain>
    return f"<{str(uuid.uuid4())}@{domain}>"


def apply_profile(raw_mime: str, profile_name: str, from_email: str = "") -> str:
    if profile_name == "default" or profile_name not in PROFILES:
        return raw_mime

    domain = from_email.split("@")[1] if "@" in from_email else "mail.local"

    if "\r\n\r\n" in raw_mime:
        header_part, body = raw_mime.split("\r\n\r\n", 1)
    else:
        return raw_mime

    headers = header_part.split("\r\n")

    if profile_name == "outlook_desktop":
        return _apply_outlook(headers, body, domain)
    elif profile_name == "apple_mail_mac":
        return _apply_apple_mac(headers, body, domain)
    elif profile_name == "apple_mail_iphone":
        return _apply_apple_iphone(headers, body, domain)
    elif profile_name == "thunderbird":
        return _apply_thunderbird(headers, body, domain)

    return raw_mime


def _extract_header(headers, name):
    for h in headers:
        if h.lower().startswith(name.lower() + ":"):
            return h
    return None


def _get_header_value(headers, name):
    h = _extract_header(headers, name)
    if h:
        return h.split(":", 1)[1].strip()
    return ""


def _replace_boundaries(text, old_boundaries, gen_func):
    mapping = {}
    for old_b in old_boundaries:
        new_b = gen_func()
        mapping[old_b] = new_b
        text = text.replace(old_b, new_b)
    return text, mapping


def _apply_outlook(headers, body, domain):
    # Outlook header order: From, To, Subject, Date, Message-ID, MIME-Version,
    #   Content-Type, X-Mailer, Thread-Index, Content-Language
    date_val = _get_header_value(headers, "Date")
    from_val = _get_header_value(headers, "From")
    to_val = _get_header_value(headers, "To")
    subject_val = _get_header_value(headers, "Subject")
    ct_header = _extract_header(headers, "Content-Type")

    new_mid = _gen_outlook_message_id(domain)
    thread_idx = _gen_outlook_thread_index()

    # Replace boundaries
    full = (ct_header or "") + "\r\n" + body
    old_bounds = re.findall(r'boundary="([^"]+)"', full)
    full, _ = _replace_boundaries(full, old_bounds, _gen_outlook_boundary)

    if ct_header and old_bounds:
        ct_line = ct_header
        for ob in old_bounds:
            if ob in ct_line:
                ct_line = ct_line.replace(ob, _gen_outlook_boundary())
        new_ct = full.split("\r\n")[0] if "\r\n" in full else ct_header
        body = "\r\n".join(full.split("\r\n")[1:]) if "\r\n" in full else body
    else:
        new_ct = ct_header or "Content-Type: text/plain"

    # Rebuild with boundary replacements applied to body too
    old_bounds2 = re.findall(r'boundary="([^"]+)"', new_ct)
    for ob in old_bounds:
        body = body.replace(ob, [v for k, v in _.items() if k == ob][0] if ob in _ else ob)

    new_headers = [
        f"From: {from_val}",
        f"To: {to_val}",
        f"Subject: {subject_val}",
        f"Date: {date_val}",
        f"Message-ID: {new_mid}",
        "MIME-Version: 1.0",
        new_ct,
        "X-Mailer: Microsoft Outlook 16.0",
        f"Thread-Index: {thread_idx}",
        "Content-Language: de",
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def _apply_apple_mac(headers, body, domain):
    # Apple Mac order: Content-Type, Content-Transfer-Encoding, From,
    #   Mime-Version, Subject, Message-Id, Date, To
    date_val = _get_header_value(headers, "Date")
    from_val = _get_header_value(headers, "From")
    to_val = _get_header_value(headers, "To")
    subject_val = _get_header_value(headers, "Subject")
    ct_header = _extract_header(headers, "Content-Type")

    new_mid = _gen_apple_message_id(domain)

    # Replace boundaries
    old_bounds = re.findall(r'boundary="?([^"\r\n]+)"?', (ct_header or "") + body)
    new_bound = _gen_apple_boundary()

    new_ct = ct_header or "Content-Type: multipart/alternative"
    for ob in old_bounds:
        ob_clean = ob.strip('"')
        new_ct = new_ct.replace(ob_clean, new_bound)
        body = body.replace(ob_clean, new_bound)

    new_headers = [
        new_ct,
        "Content-Transfer-Encoding: 7bit",
        f"From: {from_val}",
        "Mime-Version: 1.0 (1.0)",
        f"Subject: {subject_val}",
        f"Message-Id: {new_mid}",
        f"Date: {date_val}",
        f"To: {to_val}",
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def _apply_apple_iphone(headers, body, domain):
    # Same as Mac but with X-Mailer and slightly different order
    date_val = _get_header_value(headers, "Date")
    from_val = _get_header_value(headers, "From")
    to_val = _get_header_value(headers, "To")
    subject_val = _get_header_value(headers, "Subject")
    ct_header = _extract_header(headers, "Content-Type")

    new_mid = _gen_apple_message_id(domain)
    new_bound = _gen_apple_boundary()

    old_bounds = re.findall(r'boundary="?([^"\r\n]+)"?', (ct_header or "") + body)
    new_ct = ct_header or "Content-Type: multipart/alternative"
    for ob in old_bounds:
        ob_clean = ob.strip('"')
        new_ct = new_ct.replace(ob_clean, new_bound)
        body = body.replace(ob_clean, new_bound)

    ios_versions = ["22G100", "22F82", "22E252", "22D72", "22C161"]
    x_mailer = f"iPhone Mail ({random.choice(ios_versions)})"

    new_headers = [
        new_ct,
        "Content-Transfer-Encoding: 7bit",
        f"From: {from_val}",
        "Mime-Version: 1.0 (1.0)",
        f"Date: {date_val}",
        f"Subject: {subject_val}",
        f"Message-Id: {new_mid}",
        f"To: {to_val}",
        f"X-Mailer: {x_mailer}",
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def _apply_thunderbird(headers, body, domain):
    # Thunderbird order: Message-ID, Date, MIME-Version, User-Agent,
    #   To, From, Subject, Content-Type
    date_val = _get_header_value(headers, "Date")
    from_val = _get_header_value(headers, "From")
    to_val = _get_header_value(headers, "To")
    subject_val = _get_header_value(headers, "Subject")
    ct_header = _extract_header(headers, "Content-Type")

    new_mid = _gen_thunderbird_message_id(domain)

    new_headers = [
        f"Message-ID: {new_mid}",
        f"Date: {date_val}",
        "MIME-Version: 1.0",
        "User-Agent: Mozilla Thunderbird",
        f"To: {to_val}",
        f"From: {from_val}",
        f"Subject: {subject_val}",
        ct_header or "Content-Type: text/plain; charset=UTF-8",
    ]

    return "\r\n".join(new_headers) + "\r\n\r\n" + body


def get_random_profile() -> str:
    return random.choice(list(PROFILES.keys()))


def get_profile_names() -> list:
    return [(k, v.get("description", k)) for k, v in PROFILES.items()]
