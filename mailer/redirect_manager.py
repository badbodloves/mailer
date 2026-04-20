import json
import random
import sqlite3
import threading
import logging
import time
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

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
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Accept": "*/*",
    "Accept-Language": "de,en-US;q=0.7,en;q=0.3",
    "Referer": "https://www.google.com/",
}


class RedirectManager:
    def __init__(self, target_url: str = "", db_path: str = "redirects.db",
                 enabled: bool = False, rotate_every: int = 10,
                 gen_threads: int = 3):
        self._target_url = target_url
        self._db_path = db_path
        self._enabled = enabled and bool(target_url)
        self._links: List[str] = []
        self._lock = threading.Lock()
        self._gen_thread: Optional[threading.Thread] = None
        self._rotate_every = max(1, rotate_every)
        self._gen_threads = max(1, gen_threads)
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

    def _ensure_schema(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("""CREATE TABLE IF NOT EXISTS redirect_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_url TEXT NOT NULL,
            target_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        conn.close()

    def _load_from_db(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        rows = conn.execute("SELECT short_url FROM redirect_links ORDER BY id").fetchall()
        conn.close()
        with self._lock:
            self._links = [r[0] for r in rows]

    def _save_link(self, short_url: str, target: str = ""):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)",
                     (short_url, target or self._target_url))
        conn.commit()
        conn.close()

    def prepare(self, lead_count: int) -> None:
        if not self._enabled:
            return
        if not HAS_REQUESTS:
            logger.error("requests not installed")
            return
        needed = max(1, lead_count // self._rotate_every)
        current = self.pool_size
        if current >= needed:
            return
        missing = needed - current
        self._gen_thread = threading.Thread(target=self._generate_batch,
                                            args=(missing,), daemon=True)
        self._gen_thread.start()

    def wait_ready(self):
        if self._gen_thread and self._gen_thread.is_alive():
            self._gen_thread.join()

    def get_link(self, send_index: int) -> str:
        with self._lock:
            if not self._links:
                return self._target_url
            group = send_index // self._rotate_every
            return self._links[group % len(self._links)]

    def _generate_batch(self, count: int):
        print(f"  Redirect links: generating {count} ...")
        generated = 0
        for i in range(count):
            url = self._generate_one(self._target_url)
            if url:
                with self._lock:
                    self._links.append(url)
                self._save_link(url)
                generated += 1
            if (i + 1) % 10 == 0 or (i + 1) == count:
                print(f"    [{i + 1}/{count}] ({generated} ok)")
            time.sleep(0.5)
        print(f"  Redirect pool: {self.pool_size} links")

    @staticmethod
    def _generate_one(target_url: str) -> Optional[str]:
        reqpld = json.dumps([[[target_url], 1, None, None, None, None, 35]])
        params = {
            "sca_esv": "2f77f72a12157cd0",
            "client": "firefox-b-d",
            "hs": "VrYp",
            "reqpld": reqpld,
            "msc": "gwsrpc",
            "opi": "89978449",
        }
        try:
            resp = _requests.get(API_URL, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            raw = resp.text
            if raw.startswith(")]}'"):
                raw = raw[4:].strip()
            data = json.loads(raw)
            return data[0][0][0]
        except Exception as exc:
            logger.error("Redirect API error: %s", exc)
            return None

    @staticmethod
    def generate_batch_threaded(target_url: str, count: int, threads: int = 5,
                                 callback=None) -> List[str]:
        results: List[str] = []
        lock = threading.Lock()
        done = [0]

        def worker():
            url = RedirectManager._generate_one(target_url)
            with lock:
                if url:
                    results.append(url)
                done[0] += 1
                if callback:
                    callback(done[0], count, url)

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(worker) for _ in range(count)]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass
                time.sleep(0.3)
        return results
