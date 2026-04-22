"""Warmup Provider Config — IMAP/SMTP settings and folder names per provider."""

PROVIDERS = {
    "t-online": {
        "imap_host": "secureimap.t-online.de",
        "imap_port": 993,
        "smtp_host": "securesmtp.t-online.de",
        "smtp_port": 465,
        "spam_folder": "Spam",
        "auth": "password",
    },
    "gmx": {
        "imap_host": "imap.gmx.net",
        "imap_port": 993,
        "smtp_host": "mail.gmx.net",
        "smtp_port": 587,
        "spam_folder": "Spamverdacht",
        "auth": "password",
    },
    "web.de": {
        "imap_host": "imap.web.de",
        "imap_port": 993,
        "smtp_host": "smtp.web.de",
        "smtp_port": 587,
        "spam_folder": "Spamverdacht",
        "auth": "password",
    },
    "strato": {
        "imap_host": "imap.strato.de",
        "imap_port": 993,
        "smtp_host": "smtp.strato.de",
        "smtp_port": 465,
        "spam_folder": "Spam",
        "auth": "password",
    },
    "gmail": {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "spam_folder": "[Gmail]/Spam",
        "auth": "app_password",
    },
    "outlook": {
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "spam_folder": "Junk",
        "auth": "app_password",
    },
    "yahoo": {
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 587,
        "spam_folder": "Bulk Mail",
        "auth": "app_password",
    },
    "freenet": {
        "imap_host": "mx.freenet.de",
        "imap_port": 993,
        "smtp_host": "mx.freenet.de",
        "smtp_port": 587,
        "spam_folder": "Spam",
        "auth": "password",
    },
    "mailbox.org": {
        "imap_host": "imap.mailbox.org",
        "imap_port": 993,
        "smtp_host": "smtp.mailbox.org",
        "smtp_port": 587,
        "spam_folder": "Junk",
        "auth": "password",
    },
    "posteo": {
        "imap_host": "posteo.de",
        "imap_port": 993,
        "smtp_host": "posteo.de",
        "smtp_port": 587,
        "spam_folder": "Spam",
        "auth": "password",
    },
}

WARMUP_CURVES = {
    "turbo": [
        (1, 2, 50, 100),
        (3, 4, 150, 80),
        (5, 7, 400, 60),
        (8, 10, 1000, 40),
        (11, 14, 2500, 25),
        (15, 18, 5000, 15),
        (19, 21, 10000, 10),
        (22, 28, 25000, 5),
        (29, 999, 50000, 3),
    ],
    "conservative": [
        (1, 3, 20, 100),
        (4, 7, 50, 90),
        (8, 14, 200, 70),
        (15, 21, 500, 50),
        (22, 30, 1500, 30),
        (31, 45, 5000, 15),
        (46, 60, 15000, 8),
        (61, 999, 50000, 3),
    ],
}


def get_curve_values(curve_type: str, day: int) -> tuple:
    """Returns (daily_target, seed_pct) for a given day in the warmup curve."""
    curve = WARMUP_CURVES.get(curve_type, WARMUP_CURVES["turbo"])
    for start, end, volume, pct in curve:
        if start <= day <= end:
            return volume, pct
    last = curve[-1]
    return last[2], last[3]


def get_provider_config(provider: str) -> dict:
    return PROVIDERS.get(provider.lower(), PROVIDERS.get("strato", {}))


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

REPLY_TEMPLATES_DE = [
    "Danke für die Info!",
    "Hab ich gesehen, danke.",
    "Interessant, werde ich mir anschauen.",
    "Vielen Dank für die Nachricht.",
    "Danke, sehr hilfreich.",
    "Super, danke für die Zusendung!",
    "Danke schön!",
    "Habe ich zur Kenntnis genommen.",
    "Perfekt, danke!",
    "Vielen Dank, sehr nützlich.",
]
