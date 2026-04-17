import sqlite3
import threading
from typing import List, Tuple

from .utils import resolve_txt_paths, extract_emails


class DBManager:
    STATE_PENDING = "PENDING"
    STATE_SENT = "SENT"
    STATE_FAILED = "FAILED"

    def __init__(self, db_path: str = "mailer.db"):
        self._db_path = db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=10000")
        return self._local.conn

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    error_msg TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state)
            """)
            conn.commit()
            self._initialized = True

    def load_leads(self, leads_path: str) -> int:
        file_paths = resolve_txt_paths(leads_path)
        if not file_paths:
            return 0

        all_emails: list = []
        seen: set = set()
        for fpath in file_paths:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        for email in extract_emails(line):
                            if email not in seen:
                                seen.add(email)
                                all_emails.append(email)
            except OSError:
                continue

        if not all_emails:
            return 0

        conn = self._get_conn()
        inserted = 0
        batch_size = 500
        for i in range(0, len(all_emails), batch_size):
            batch = all_emails[i : i + batch_size]
            try:
                before = self._count_total(conn)
                conn.executemany(
                    "INSERT OR IGNORE INTO leads (email, state) VALUES (?, ?)",
                    [(e, self.STATE_PENDING) for e in batch],
                )
                conn.commit()
                after = self._count_total(conn)
                inserted += after - before
            except sqlite3.Error:
                conn.rollback()
        return inserted

    @staticmethod
    def _count_total(conn: sqlite3.Connection) -> int:
        cursor = conn.execute("SELECT COUNT(*) FROM leads")
        return cursor.fetchone()[0]

    def fetch_pending_batch(self, batch_size: int = 100) -> List[Tuple[int, str]]:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT id, email FROM leads WHERE state = ? ORDER BY id LIMIT ?",
            (self.STATE_PENDING, batch_size),
        )
        return cursor.fetchall()

    def mark_sent(self, lead_id: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE leads SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (self.STATE_SENT, lead_id),
        )
        conn.commit()

    def mark_failed(self, lead_id: int, error_msg: str = "") -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE leads SET state = ?, error_msg = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (self.STATE_FAILED, error_msg[:500], lead_id),
        )
        conn.commit()

    def mark_in_progress(self, lead_ids: List[int]) -> None:
        if not lead_ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in lead_ids)
        conn.execute(
            f"UPDATE leads SET state = 'IN_PROGRESS', updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            lead_ids,
        )
        conn.commit()

    def reset_in_progress(self) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE leads SET state = ? WHERE state = 'IN_PROGRESS'",
            (self.STATE_PENDING,),
        )
        conn.commit()

    def count_by_state(self) -> dict:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT state, COUNT(*) FROM leads GROUP BY state"
        )
        counts = {self.STATE_PENDING: 0, self.STATE_SENT: 0, self.STATE_FAILED: 0, "IN_PROGRESS": 0}
        for state, count in cursor.fetchall():
            counts[state] = count
        return counts

    def total_count(self) -> int:
        conn = self._get_conn()
        return self._count_total(conn)

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
