"""Score-Presets & Auto-Provisioning Helpers.

Presets: klick statt Zahlen tunen. Der Gate speichert nur den Namen
(light|medium|hard|max) und beim Scoring werden daraus die konkreten
Schwellwerte gezogen. Damit kann man später den Preset einer Domain
umschalten ohne die Regeln neu zu tippen.
"""
import logging
import secrets
import string
import requests as _req

logger = logging.getLogger("antibot.presets")

MODE_PRESETS = {
    "light":  {"threshold_allow": 60, "threshold_block": 90,
               "pow_difficulty": 3,  "rate_limit_per_min": 120,
               "label": "Leicht (mildes Filtering — wenige False-Positives)"},
    "medium": {"threshold_allow": 40, "threshold_block": 70,
               "pow_difficulty": 5,  "rate_limit_per_min": 60,
               "label": "Mittel (Empfehlung — ausgewogen)"},
    "hard":   {"threshold_allow": 25, "threshold_block": 55,
               "pow_difficulty": 6,  "rate_limit_per_min": 30,
               "label": "Hart (aggressiv — Cloud-IPs bekommen fast immer Challenge)"},
    "max":    {"threshold_allow": 15, "threshold_block": 40,
               "pow_difficulty": 7,  "rate_limit_per_min": 15,
               "label": "Maximal (nur echte Wohn-IPs kommen ohne Challenge durch)"},
}


def resolve_mode(mode: str) -> dict:
    return MODE_PRESETS.get(mode, MODE_PRESETS["medium"])


def gen_slug(n: int = 8) -> str:
    """Random URL-safe slug — alphanumerisch damit's in Text-Mails
    nicht zerlegt wird."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def detect_public_ip() -> str:
    """Ermittelt die eigene öffentliche IP über ifconfig.io.
    Wird für Auto-CF-Connect gebraucht."""
    for url in ("https://ifconfig.io/ip", "https://api.ipify.org",
                "https://icanhazip.com"):
        try:
            r = _req.get(url, timeout=5)
            if r.status_code == 200:
                ip = r.text.strip().split()[0]
                if ip and all(p.isdigit() and 0 <= int(p) <= 255
                              for p in ip.split(".")):
                    return ip
        except Exception as e:
            logger.debug("ip lookup %s failed: %s", url, e)
    return ""
