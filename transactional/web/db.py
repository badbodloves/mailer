"""Transactional Mailer Web DB — replaces config.ini with SQLite."""
import os
import json
import sqlite3
import threading
from typing import List, Tuple


class TransDB:
    def __init__(self, db_path: str = "trans.db"):
        self._db_path = db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _ensure_schema(self):
        with self._init_lock:
            if self._initialized:
                return
            c = self._conn()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS trans_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    settings_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS trans_smtps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 587,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    proxy TEXT DEFAULT '',
                    daily_limit INTEGER DEFAULT 0,
                    sent_today INTEGER DEFAULT 0,
                    warmup_sent INTEGER DEFAULT 0,
                    is_dead INTEGER DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS trans_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    status TEXT DEFAULT 'DRAFT',
                    from_name TEXT DEFAULT '',
                    from_email TEXT DEFAULT '',
                    subject TEXT DEFAULT '',
                    threads INTEGER DEFAULT 40,
                    normal_delay REAL DEFAULT 0.3,
                    provider_delay REAL DEFAULT 6.0,
                    warmup_delay REAL DEFAULT 30.0,
                    warmup_count INTEGER DEFAULT 5,
                    test_recipients TEXT DEFAULT '',
                    test_interval INTEGER DEFAULT 0,
                    schedule_time TEXT DEFAULT '',
                    antifingerprint_classes INTEGER DEFAULT 1,
                    advanced_antifingerprint INTEGER DEFAULT 1,
                    structure_variation REAL DEFAULT 0.5,
                    image_enabled INTEGER DEFAULT 0,
                    image_mode TEXT DEFAULT 'cid',
                    image_quantize INTEGER DEFAULT 1,
                    image_downscale INTEGER DEFAULT 0,
                    logo_max_colors INTEGER DEFAULT 256,
                    logo_rotate_every INTEGER DEFAULT 0,
                    redirect_enabled INTEGER DEFAULT 0,
                    redirect_target_url TEXT DEFAULT '',
                    redirect_rotate_every INTEGER DEFAULT 10,
                    proxy_rotate_every INTEGER DEFAULT 0,
                    ignore_ssl_errors INTEGER DEFAULT 1,
                    total_leads INTEGER DEFAULT 0,
                    sent INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS trans_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL REFERENCES trans_campaigns(id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    state TEXT DEFAULT 'PENDING',
                    error_msg TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_tl_state ON trans_leads(state);
                CREATE INDEX IF NOT EXISTS idx_tl_campaign ON trans_leads(campaign_id);

                CREATE TABLE IF NOT EXISTS trans_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    html_content TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS trans_content_pools (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pool_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(pool_type, name)
                );

                CREATE TABLE IF NOT EXISTS trans_logos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS trans_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    logo_path TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            c.commit()
            self._initialized = True

    # --- Config ---
    def get_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT settings_json FROM trans_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO trans_config (id, settings_json) VALUES (1, '{}')")
            c.commit()
            return {}
        return json.loads(r["settings_json"] or "{}")

    def save_config(self, settings: dict):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO trans_config (id, settings_json) VALUES (1, ?)",
                  (json.dumps(settings, ensure_ascii=False),))
        c.commit()

    # --- SMTPs ---
    def add_smtp(self, host: str, port: int, username: str, password: str,
                 proxy: str = "", daily_limit: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_smtps (host,port,username,password,proxy,daily_limit) "
                  "VALUES (?,?,?,?,?,?)", (host, port, username, password, proxy, daily_limit))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def import_smtps(self, text: str) -> int:
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            host, port_s, user, pwd = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
            proxy = parts[4].strip() if len(parts) > 4 else ""
            try:
                self.add_smtp(host, int(port_s), user, pwd, proxy)
                added += 1
            except Exception:
                pass
        return added

    def get_smtps(self) -> list:
        return self._conn().execute("SELECT * FROM trans_smtps ORDER BY host").fetchall()

    def delete_smtp(self, sid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_smtps WHERE id=?", (sid,))
        c.commit()

    def update_smtp(self, sid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_smtps SET {sets} WHERE id=?", list(kw.values()) + [sid])
        c.commit()

    # --- Campaigns ---
    def create_campaign(self, **kw) -> int:
        c = self._conn()
        cols = list(kw.keys())
        vals = list(kw.values())
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO trans_campaigns ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_campaigns(self) -> list:
        return self._conn().execute("SELECT * FROM trans_campaigns ORDER BY created_at DESC").fetchall()

    def get_campaign(self, cid: int):
        return self._conn().execute("SELECT * FROM trans_campaigns WHERE id=?", (cid,)).fetchone()

    def update_campaign(self, cid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_campaigns SET {sets} WHERE id=?", list(kw.values()) + [cid])
        c.commit()

    def delete_campaign(self, cid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_campaigns WHERE id=?", (cid,))
        c.commit()

    # --- Leads ---
    def import_leads(self, campaign_id: int, emails: list) -> int:
        c = self._conn()
        added = 0
        batch = []
        for email in emails:
            email = email.strip()
            if not email or "@" not in email:
                continue
            batch.append((campaign_id, email))
            if len(batch) >= 500:
                c.executemany("INSERT INTO trans_leads (campaign_id, email) VALUES (?,?)", batch)
                added += len(batch)
                batch = []
        if batch:
            c.executemany("INSERT INTO trans_leads (campaign_id, email) VALUES (?,?)", batch)
            added += len(batch)
        c.execute("UPDATE trans_campaigns SET total_leads=? WHERE id=?", (added, campaign_id))
        c.commit()
        return added

    def get_lead_count(self, campaign_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_leads WHERE campaign_id=?",
                                  (campaign_id,)).fetchone()
        return r[0] if r else 0

    def get_lead_states(self, campaign_id: int) -> dict:
        rows = self._conn().execute(
            "SELECT state, COUNT(*) FROM trans_leads WHERE campaign_id=? GROUP BY state",
            (campaign_id,)).fetchall()
        states = {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0}
        for r in rows:
            states[r[0]] = r[1]
        return states

    def fetch_pending(self, campaign_id: int, batch_size: int = 200):
        return self._conn().execute(
            "SELECT id, email FROM trans_leads WHERE campaign_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (campaign_id, batch_size)).fetchall()

    def mark_sent(self, lead_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='SENT' WHERE id=?", (lead_id,))
        c.commit()

    def mark_failed(self, lead_id: int, error: str = ""):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='FAILED', error_msg=? WHERE id=?", (error[:500], lead_id))
        c.commit()

    def mark_in_progress(self, lead_ids: list):
        if not lead_ids:
            return
        c = self._conn()
        ph = ",".join("?" for _ in lead_ids)
        c.execute(f"UPDATE trans_leads SET state='IN_PROGRESS' WHERE id IN ({ph})", lead_ids)
        c.commit()

    def reset_leads(self, campaign_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING', error_msg='' WHERE campaign_id=?",
                  (campaign_id,))
        c.execute("UPDATE trans_campaigns SET sent=0, failed=0 WHERE id=?", (campaign_id,))
        c.commit()

    def reset_in_progress(self, campaign_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING' WHERE campaign_id=? AND state='IN_PROGRESS'",
                  (campaign_id,))
        c.commit()

    # --- Templates ---
    def add_template(self, name: str, html_content: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_templates (name, html_content) VALUES (?,?)",
                  (name, html_content))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_templates(self) -> list:
        return self._conn().execute("SELECT * FROM trans_templates ORDER BY name").fetchall()

    def update_template(self, tid: int, name: str, html_content: str):
        c = self._conn()
        c.execute("UPDATE trans_templates SET name=?, html_content=? WHERE id=?",
                  (name, html_content, tid))
        c.commit()

    def delete_template(self, tid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_templates WHERE id=?", (tid,))
        c.commit()

    # --- Content Pools ---
    def set_pool(self, pool_type: str, name: str, content: str):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO trans_content_pools (pool_type, name, content) VALUES (?,?,?)",
                  (pool_type, name, content))
        c.commit()

    def get_pool(self, pool_type: str, name: str = "default") -> str:
        r = self._conn().execute(
            "SELECT content FROM trans_content_pools WHERE pool_type=? AND name=?",
            (pool_type, name)).fetchone()
        return r["content"] if r else ""

    def get_pools(self, pool_type: str) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_content_pools WHERE pool_type=? ORDER BY name",
            (pool_type,)).fetchall()

    def delete_pool(self, pid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_content_pools WHERE id=?", (pid,))
        c.commit()

    # --- Users (shared auth) ---
    def get_user(self, username: str):
        return self._conn().execute(
            "SELECT * FROM trans_users WHERE username=?", (username,)).fetchone()

    def get_user_by_id(self, uid: int):
        return self._conn().execute(
            "SELECT * FROM trans_users WHERE id=?", (uid,)).fetchone()

    def create_user(self, username: str, password_hash: str, display_name: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_users (username, password_hash, display_name) VALUES (?,?,?)",
                  (username, password_hash, display_name))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def user_count(self) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_users").fetchone()
        return r[0] if r else 0

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
