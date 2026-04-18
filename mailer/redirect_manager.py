import json
import random
import sqlite3
import threading
import logging
from typing import List, Optional

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("mailer.redirect")

API_URL = (
    "https://www.google.com/httpservice/retry/"
    "SearchApiService/GetShortenedKpSharingUrl"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)


class RedirectManager:
    LINKS_PER_GROUP = 10

    def __init__(
        self,
        target_url: str = "",
        db_path: str = "redirects.db",
        enabled: bool = False,
    ):
        self._target_url = target_url
        self._db_path = db_path
        self._enabled = enabled and bool(target_url)
        self._links: List[str] = []
        self._lock = threading.Lock()
        self._gen_thread: Optional[threading.Thread] = None
        if self._enabled:
            self._ensure_schema()
            self._load_from_db()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pool_size(self) -> int:
        with self._lock:
            return len(self._links)

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS redirect_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                short_url TEXT NOT NULL,
                target_url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def _load_from_db(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=10)
        rows = conn.execute(
            "SELECT short_url FROM redirect_links ORDER BY id"
        ).fetchall()
        conn.close()
        with self._lock:
            self._links = [r[0] for r in rows]

    def _save_link(self, short_url: str) -> None:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute(
            "INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)",
            (short_url, self._target_url),
        )
        conn.commit()
        conn.close()

    def prepare(self, lead_count: int) -> None:
        if not self._enabled:
            return
        if not HAS_REQUESTS:
            logger.error("requests not installed — redirect generation unavailable")
            return

        needed = max(1, lead_count // self.LINKS_PER_GROUP)
        current = self.pool_size
        if current >= needed:
            return

        missing = needed - current
        self._gen_thread = threading.Thread(
            target=self._generate_batch,
            args=(missing,),
            daemon=True,
        )
        self._gen_thread.start()

    def wait_ready(self) -> None:
        if self._gen_thread and self._gen_thread.is_alive():
            self._gen_thread.join()

    def get_link(self, send_index: int) -> str:
        with self._lock:
            if not self._links:
                return self._target_url
            group = send_index // self.LINKS_PER_GROUP
            idx = group % len(self._links)
            return self._links[idx]

    def _generate_batch(self, count: int) -> None:
        print(f"  Redirect links: generating {count} ...")
        generated = 0
        for i in range(count):
            url = self._generate_one()
            if url:
                with self._lock:
                    self._links.append(url)
                self._save_link(url)
                generated += 1
            if (i + 1) % 10 == 0 or (i + 1) == count:
                print(f"    [{i + 1}/{count}] generated")
        print(f"  Redirect pool ready: {self.pool_size} links")

    def _generate_one(self) -> Optional[str]:
        rand_param = random.randint(100000, 999999)
        target = f"{self._target_url}?_r={rand_param}"
        payload = json.dumps([target])

        try:
            resp = _requests.post(
                API_URL,
                params={
                    "reqpld": payload,
                    "msc": "gwsrpc",
                    "client": "firefox-b-d",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error("Redirect API %d: %s", resp.status_code, resp.text[:200])
                return None

            body = resp.text
            if "share.google" in body:
                for token in body.replace('"', " ").replace("'", " ").split():
                    if "share.google" in token and token.startswith("http"):
                        return token.strip()

            try:
                data = resp.json()
                if isinstance(data, list) and data:
                    return str(data[0])
                if isinstance(data, str):
                    return data
            except (ValueError, KeyError):
                pass

            logger.error("Redirect API: unexpected response format: %s", body[:300])
            return None
        except Exception as exc:
            logger.error("Redirect API error: %s", exc)
            return None
