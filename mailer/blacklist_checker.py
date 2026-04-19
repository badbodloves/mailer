"""MXToolbox Blacklist Checker.

Checks whether an IP or domain is listed on any major email blacklist
using the MXToolbox API.
"""
import logging
from typing import List, Optional, Tuple

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("mailer.blacklist")

API_BASE = "https://mxtoolbox.com/api/v1/lookup"


class BlacklistChecker:
    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        self._enabled = bool(api_key) and HAS_REQUESTS

    @property
    def enabled(self) -> bool:
        return self._enabled

    def check(self, host: str) -> Tuple[bool, List[dict]]:
        if not self._enabled:
            return True, [{"error": "API key not configured or requests not installed"}]
        url = f"{API_BASE}/blacklist/{host}"
        headers = {"Authorization": self._api_key}
        try:
            resp = _requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                failures = []
                passed = []
                for entry in data.get("Failed", []):
                    failures.append({
                        "name": entry.get("Name", ""),
                        "info": entry.get("Info", ""),
                    })
                is_clean = len(failures) == 0
                return is_clean, failures
            logger.error("MXToolbox API %d: %s", resp.status_code, resp.text[:200])
            return True, [{"error": f"API returned {resp.status_code}"}]
        except Exception as exc:
            logger.error("MXToolbox error: %s", exc)
            return True, [{"error": str(exc)}]

    def check_smtp_accounts(self, accounts: list) -> dict:
        results = {}
        seen = set()
        for acc in accounts:
            host = acc.host if hasattr(acc, "host") else str(acc)
            if host in seen:
                continue
            seen.add(host)
            is_clean, details = self.check(host)
            results[host] = {"clean": is_clean, "details": details}
        return results
