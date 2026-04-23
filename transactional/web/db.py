"""Transactional Mailer Web DB — Lists-based architecture."""
import os
import json
import sqlite3
import threading


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
                CREATE TABLE IF NOT EXISTS trans_smtp_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_smtps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER NOT NULL REFERENCES trans_smtp_lists(id) ON DELETE CASCADE,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 587,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    is_dead INTEGER DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_ts_list ON trans_smtps(list_id);
                CREATE TABLE IF NOT EXISTS trans_lead_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_origin TEXT DEFAULT '',
                    lead_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER NOT NULL REFERENCES trans_lead_lists(id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    state TEXT DEFAULT 'PENDING',
                    error_msg TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_tlead_state ON trans_leads(state);
                CREATE INDEX IF NOT EXISTS idx_tlead_list ON trans_leads(list_id);
                CREATE TABLE IF NOT EXISTS trans_macros (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    values_text TEXT DEFAULT '',
                    rotate_every INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    html_content TEXT DEFAULT '',
                    rotate_every INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_template_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id INTEGER NOT NULL REFERENCES trans_templates(id) ON DELETE CASCADE,
                    filename TEXT DEFAULT '',
                    html_content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_logos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_redirect_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    short_url TEXT NOT NULL,
                    target_url TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    status TEXT DEFAULT 'DRAFT',
                    smtp_list_id INTEGER REFERENCES trans_smtp_lists(id),
                    lead_list_id INTEGER REFERENCES trans_lead_lists(id),
                    schedule_time TEXT DEFAULT '',
                    total_leads INTEGER DEFAULT 0,
                    sent INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    proxy_type TEXT DEFAULT 'single',
                    value TEXT DEFAULT '',
                    rotate_every INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trans_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            c.commit()
            self._migrate(c)
            self._initialized = True

    def _migrate(self, c):
        """Auto-add missing tables and columns so DB never needs deletion."""
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "trans_template_files" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_template_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL REFERENCES trans_templates(id) ON DELETE CASCADE,
                filename TEXT DEFAULT '', html_content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_proxies" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                proxy_type TEXT DEFAULT 'single', value TEXT DEFAULT '',
                rotate_every INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_redirect_links" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_redirect_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT, short_url TEXT NOT NULL,
                target_url TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_macros" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_macros (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                values_text TEXT DEFAULT '', rotate_every INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        if "trans_logos" not in tables:
            c.execute("""CREATE TABLE IF NOT EXISTS trans_logos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        for tbl, col, default in [
            ("trans_templates", "rotate_every", "0"),
            ("trans_campaigns", "started_at", "NULL"),
            ("trans_campaigns", "finished_at", "NULL"),
            ("trans_campaigns", "schedule_time", "''"),
        ]:
            if tbl in tables:
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if col not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} DEFAULT {default}")
        c.commit()

    # ── Config (single row JSON) ──────────────────────────
    _DEFAULTS = {
        "threads": 40, "normal_delay": 0.3, "provider_delay": 6.0,
        "warmup_delay": 30.0, "warmup_count": 5, "smtp_timeout": 30,
        "ignore_ssl_errors": True, "schedule_time": "",
        "from_name": "", "from_email": "", "subject": "",
        "test_recipients": "", "test_interval": 0, "interval_recipients": "",
        "antifingerprint_classes": True, "advanced_antifingerprint": True,
        "structure_variation": 0.5,
        "image_enabled": False, "image_mode": "cid", "image_quantize": True,
        "image_downscale": False, "logo_max_colors": 256, "logo_rotate_every": 0,
        "cloudinary_cloud_name": "", "cloudinary_api_key": "", "cloudinary_api_secret": "",
        "redirect_enabled": False, "redirect_target_url": "",
        "redirect_rotate_every": 10, "redirect_gen_threads": 3,
        "proxy_mode": "off", "proxy_value": "", "proxy_rotate_every": 0,
        "mxtoolbox_api_key": "",
        "html_rotate_every": 0,
        "llm_api_url": "https://openrouter.ai/api/v1/chat/completions",
        "llm_api_key": "",
        "llm_model": "anthropic/claude-sonnet-4-20250514",
    }

    def get_config(self) -> dict:
        c = self._conn()
        r = c.execute("SELECT settings_json FROM trans_config WHERE id=1").fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO trans_config (id,settings_json) VALUES (1,?)",
                      (json.dumps(self._DEFAULTS),))
            c.commit()
            return dict(self._DEFAULTS)
        cfg = dict(self._DEFAULTS)
        cfg.update(json.loads(r["settings_json"] or "{}"))
        return cfg

    def save_config(self, settings: dict):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO trans_config (id,settings_json) VALUES (1,?)",
                  (json.dumps(settings, ensure_ascii=False),))
        c.commit()

    def update_config(self, **kw):
        cfg = self.get_config()
        cfg.update(kw)
        self.save_config(cfg)

    # ── SMTP Lists ────────────────────────────────────────
    def create_smtp_list(self, name: str) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_smtp_lists (name) VALUES (?)", (name,))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_smtp_lists(self) -> list:
        return self._conn().execute("SELECT * FROM trans_smtp_lists ORDER BY name").fetchall()

    def delete_smtp_list(self, lid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_smtp_lists WHERE id=?", (lid,))
        c.commit()

    def import_smtps(self, list_id: int, text: str) -> int:
        c = self._conn()
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            try:
                c.execute("INSERT INTO trans_smtps (list_id,host,port,username,password) VALUES (?,?,?,?,?)",
                          (list_id, parts[0].strip(), int(parts[1].strip()),
                           parts[2].strip(), parts[3].strip()))
                added += 1
            except Exception:
                pass
        c.commit()
        return added

    def get_smtps(self, list_id: int = 0) -> list:
        if list_id:
            return self._conn().execute("SELECT * FROM trans_smtps WHERE list_id=? ORDER BY host",
                                         (list_id,)).fetchall()
        return self._conn().execute("SELECT * FROM trans_smtps ORDER BY host").fetchall()

    def get_smtp_count(self, list_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_smtps WHERE list_id=?", (list_id,)).fetchone()
        return r[0] if r else 0

    def delete_smtp(self, sid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_smtps WHERE id=?", (sid,))
        c.commit()

    def update_smtp(self, sid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_smtps SET {sets} WHERE id=?", list(kw.values()) + [sid])
        c.commit()

    def delete_smtps_by_ids(self, ids: list):
        if not ids:
            return
        c = self._conn()
        ph = ",".join("?" for _ in ids)
        c.execute(f"DELETE FROM trans_smtps WHERE id IN ({ph})", ids)
        c.commit()

    # ── Lead Lists ────────────────────────────────────────
    def create_lead_list(self, name: str, file_origin: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_lead_lists (name,file_origin) VALUES (?,?)",
                  (name, file_origin))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_lead_lists(self) -> list:
        return self._conn().execute("SELECT * FROM trans_lead_lists ORDER BY name").fetchall()

    def delete_lead_list(self, lid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_lead_lists WHERE id=?", (lid,))
        c.commit()

    def import_leads(self, list_id: int, emails: list) -> int:
        c = self._conn()
        added = 0
        batch = []
        for email in emails:
            email = email.strip()
            if not email or "@" not in email:
                continue
            batch.append((list_id, email))
            if len(batch) >= 500:
                c.executemany("INSERT INTO trans_leads (list_id,email) VALUES (?,?)", batch)
                added += len(batch)
                batch = []
        if batch:
            c.executemany("INSERT INTO trans_leads (list_id,email) VALUES (?,?)", batch)
            added += len(batch)
        c.execute("UPDATE trans_lead_lists SET lead_count=? WHERE id=?", (added, list_id))
        c.commit()
        return added

    def get_lead_count(self, list_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_leads WHERE list_id=?",
                                  (list_id,)).fetchone()
        return r[0] if r else 0

    def get_lead_states(self, list_id: int) -> dict:
        rows = self._conn().execute(
            "SELECT state, COUNT(*) FROM trans_leads WHERE list_id=? GROUP BY state",
            (list_id,)).fetchall()
        states = {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0}
        for r in rows:
            states[r[0]] = r[1]
        return states

    def fetch_pending(self, list_id: int, batch_size: int = 200):
        return self._conn().execute(
            "SELECT id, email FROM trans_leads WHERE list_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (list_id, batch_size)).fetchall()

    def mark_sent(self, lead_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='SENT' WHERE id=?", (lead_id,))
        c.commit()

    def mark_failed(self, lead_id: int, error: str = ""):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='FAILED', error_msg=? WHERE id=?",
                  (error[:500], lead_id))
        c.commit()

    def mark_in_progress(self, lead_ids: list):
        if not lead_ids:
            return
        c = self._conn()
        ph = ",".join("?" for _ in lead_ids)
        c.execute(f"UPDATE trans_leads SET state='IN_PROGRESS' WHERE id IN ({ph})", lead_ids)
        c.commit()

    def reset_leads(self, list_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING', error_msg='' WHERE list_id=?",
                  (list_id,))
        c.commit()

    def reset_in_progress(self, list_id: int):
        c = self._conn()
        c.execute("UPDATE trans_leads SET state='PENDING' WHERE list_id=? AND state='IN_PROGRESS'",
                  (list_id,))
        c.commit()

    def get_lead_preview(self, list_id: int, limit: int = 10) -> list:
        return self._conn().execute("SELECT email FROM trans_leads WHERE list_id=? LIMIT ?",
                                     (list_id, limit)).fetchall()

    # ── Macros ────────────────────────────────────────────
    def add_macro(self, name: str, values_text: str = "", rotate_every: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO trans_macros (name,values_text,rotate_every) VALUES (?,?,?)",
                  (name, values_text, rotate_every))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_macros(self) -> list:
        return self._conn().execute("SELECT * FROM trans_macros ORDER BY name").fetchall()

    def get_macro(self, name: str) -> str:
        r = self._conn().execute("SELECT values_text FROM trans_macros WHERE name=?", (name,)).fetchone()
        return r["values_text"] if r else ""

    def update_macro(self, mid: int, values_text: str, rotate_every: int = 0):
        c = self._conn()
        c.execute("UPDATE trans_macros SET values_text=?, rotate_every=? WHERE id=?",
                  (values_text, rotate_every, mid))
        c.commit()

    def delete_macro(self, mid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_macros WHERE id=?", (mid,))
        c.commit()

    # ── Templates ─────────────────────────────────────────
    def add_template(self, name: str, html_content: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_templates (name,html_content) VALUES (?,?)",
                  (name, html_content))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_templates(self) -> list:
        return self._conn().execute("SELECT * FROM trans_templates ORDER BY name").fetchall()

    def get_template(self, tid: int):
        return self._conn().execute("SELECT * FROM trans_templates WHERE id=?", (tid,)).fetchone()

    def update_template(self, tid: int, name: str, html_content: str):
        c = self._conn()
        c.execute("UPDATE trans_templates SET name=?, html_content=? WHERE id=?",
                  (name, html_content, tid))
        c.commit()

    def delete_template(self, tid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_templates WHERE id=?", (tid,))
        c.commit()

    def add_template_file(self, template_id: int, filename: str, html_content: str) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_template_files (template_id,filename,html_content) VALUES (?,?,?)",
                  (template_id, filename, html_content))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_template_files(self, template_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM trans_template_files WHERE template_id=? ORDER BY filename",
            (template_id,)).fetchall()

    def get_template_file_count(self, template_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_template_files WHERE template_id=?",
                                  (template_id,)).fetchone()
        return r[0] if r else 0

    def delete_template_file(self, fid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_template_files WHERE id=?", (fid,))
        c.commit()

    def get_all_template_htmls(self) -> list:
        """Get all HTML bodies: from template_files first, fall back to template.html_content."""
        bodies = []
        for t in self.get_templates():
            t = dict(t)
            files = self.get_template_files(t["id"])
            if files:
                for f in files:
                    bodies.append(dict(f)["html_content"])
            elif t.get("html_content", "").strip():
                bodies.append(t["html_content"])
        return bodies

    # ── Logos ─────────────────────────────────────────────
    def add_logo(self, filename: str, file_path: str) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_logos (filename,file_path) VALUES (?,?)",
                  (filename, file_path))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_logos(self) -> list:
        return self._conn().execute("SELECT * FROM trans_logos ORDER BY filename").fetchall()

    def delete_logo(self, lid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_logos WHERE id=?", (lid,))
        c.commit()

    # ── Redirect Links ────────────────────────────────────
    def add_redirect(self, short_url: str, target_url: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_redirect_links (short_url,target_url) VALUES (?,?)",
                  (short_url, target_url))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_redirects(self) -> list:
        return self._conn().execute("SELECT * FROM trans_redirect_links ORDER BY created_at DESC").fetchall()

    def get_redirect_count(self) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM trans_redirect_links").fetchone()
        return r[0] if r else 0

    def clear_redirects(self):
        c = self._conn()
        c.execute("DELETE FROM trans_redirect_links")
        c.commit()

    def delete_redirect(self, rid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_redirect_links WHERE id=?", (rid,))
        c.commit()

    # ── Proxies ────────────────────────────────────────────
    def add_proxy(self, name: str, proxy_type: str = "single",
                  value: str = "", rotate_every: int = 0) -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_proxies (name,proxy_type,value,rotate_every) VALUES (?,?,?,?)",
                  (name, proxy_type, value, rotate_every))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_proxies(self) -> list:
        return self._conn().execute("SELECT * FROM trans_proxies ORDER BY name").fetchall()

    def get_proxy(self, pid: int):
        return self._conn().execute("SELECT * FROM trans_proxies WHERE id=?", (pid,)).fetchone()

    def update_proxy(self, pid: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE trans_proxies SET {sets} WHERE id=?", list(kw.values()) + [pid])
        c.commit()

    def delete_proxy(self, pid: int):
        c = self._conn()
        c.execute("DELETE FROM trans_proxies WHERE id=?", (pid,))
        c.commit()

    # ── Campaigns ─────────────────────────────────────────
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

    # ── Users ─────────────────────────────────────────────
    def get_user(self, username: str):
        return self._conn().execute("SELECT * FROM trans_users WHERE username=?",
                                     (username,)).fetchone()

    def get_user_by_id(self, uid: int):
        return self._conn().execute("SELECT * FROM trans_users WHERE id=?", (uid,)).fetchone()

    def create_user(self, username: str, password_hash: str, display_name: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO trans_users (username,password_hash,display_name) VALUES (?,?,?)",
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
