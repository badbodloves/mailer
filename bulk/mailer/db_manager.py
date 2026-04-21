"""Bulk Mailer Database Manager.

SQLite schema for brands, domains, SMTP presets, mailing lists,
macros, message templates, and mailing state.
"""
import os
import sqlite3
import threading
import json
from typing import List, Tuple, Optional, Dict


class BulkDBManager:
    def __init__(self, db_path: str = "bulk.db"):
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
                CREATE TABLE IF NOT EXISTS brands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS domains (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    domain TEXT NOT NULL,
                    from_name TEXT DEFAULT '',
                    from_email TEXT DEFAULT '',
                    reply_to_email TEXT DEFAULT '',
                    bounce_subdomain TEXT DEFAULT 'bounce',
                    send_subdomain TEXT DEFAULT 'mail',
                    unsub_worker_deployed INTEGER DEFAULT 0,
                    unsub_domain TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(brand_id, domain)
                );

                CREATE TABLE IF NOT EXISTS smtp_presets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER DEFAULT 587,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    provider_type TEXT DEFAULT 'generic',
                    daily_limit INTEGER DEFAULT 0,
                    sent_today INTEGER DEFAULT 0,
                    last_reset_date TEXT DEFAULT '',
                    proxy TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS cf_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    auth_type TEXT DEFAULT 'token',
                    api_token TEXT DEFAULT '',
                    global_api_key TEXT DEFAULT '',
                    auth_email TEXT DEFAULT '',
                    account_id TEXT DEFAULT '',
                    r2_access_key TEXT DEFAULT '',
                    r2_secret_key TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS lead_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_origin TEXT DEFAULT '',
                    lead_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_id INTEGER NOT NULL REFERENCES lead_lists(id) ON DELETE CASCADE,
                    email TEXT NOT NULL,
                    state TEXT DEFAULT 'PENDING',
                    error_msg TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
                CREATE INDEX IF NOT EXISTS idx_leads_list ON leads(list_id);
                CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);

                CREATE TABLE IF NOT EXISTS brand_list_usage (
                    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
                    list_id INTEGER NOT NULL REFERENCES lead_lists(id) ON DELETE CASCADE,
                    mailing_id INTEGER,
                    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (brand_id, list_id)
                );

                CREATE TABLE IF NOT EXISTS macros (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    values_json TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS message_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    html_files_json TEXT DEFAULT '[]',
                    html_rotate_every INTEGER DEFAULT 0,
                    pdf_path TEXT DEFAULT '',
                    pdf_macro_enabled INTEGER DEFAULT 0,
                    subject_macro TEXT DEFAULT '',
                    sender_rotate_json TEXT DEFAULT '[]',
                    sender_rotate_every INTEGER DEFAULT 0,
                    settings_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS mailings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER REFERENCES brands(id),
                    domain_id INTEGER REFERENCES domains(id),
                    smtp_preset_id INTEGER REFERENCES smtp_presets(id),
                    list_id INTEGER REFERENCES lead_lists(id),
                    template_id INTEGER REFERENCES message_templates(id),
                    status TEXT DEFAULT 'DRAFT',
                    total_leads INTEGER DEFAULT 0,
                    sent INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    excluded INTEGER DEFAULT 0,
                    daily_limit INTEGER DEFAULT 0,
                    exclude_domains_json TEXT DEFAULT '[]',
                    started_at TIMESTAMP,
                    paused_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            c.commit()
            self._initialized = True

    # --- Brands ---
    def add_brand(self, name: str) -> int:
        c = self._conn()
        c.execute("INSERT INTO brands (name) VALUES (?)", (name,))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_brands(self) -> list:
        return self._conn().execute("SELECT * FROM brands ORDER BY name").fetchall()

    def delete_brand(self, brand_id: int):
        c = self._conn()
        c.execute("DELETE FROM brands WHERE id=?", (brand_id,))
        c.commit()

    # --- Domains ---
    def add_domain(self, brand_id: int, domain: str, **kw) -> int:
        c = self._conn()
        cols = ["brand_id", "domain"] + list(kw.keys())
        vals = [brand_id, domain] + list(kw.values())
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO domains ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_domains(self, brand_id: int = None) -> list:
        if brand_id:
            return self._conn().execute("SELECT * FROM domains WHERE brand_id=?", (brand_id,)).fetchall()
        return self._conn().execute("SELECT * FROM domains ORDER BY domain").fetchall()

    def delete_domain(self, domain_id: int):
        c = self._conn()
        c.execute("DELETE FROM domains WHERE id=?", (domain_id,))
        c.commit()

    # --- SMTP Presets ---
    def add_smtp(self, name: str, host: str, port: int, username: str,
                 password: str, provider_type: str = "generic",
                 daily_limit: int = 0, proxy: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO smtp_presets (name,host,port,username,password,provider_type,daily_limit,proxy) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (name, host, port, username, password, provider_type, daily_limit, proxy))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_smtps(self) -> list:
        return self._conn().execute("SELECT * FROM smtp_presets ORDER BY name").fetchall()

    def delete_smtp(self, smtp_id: int):
        c = self._conn()
        c.execute("DELETE FROM smtp_presets WHERE id=?", (smtp_id,))
        c.commit()

    def reset_daily_counts(self):
        import datetime
        today = datetime.date.today().isoformat()
        c = self._conn()
        c.execute("UPDATE smtp_presets SET sent_today=0, last_reset_date=? "
                  "WHERE last_reset_date != ?", (today, today))
        c.commit()

    def increment_smtp_sent(self, smtp_id: int):
        c = self._conn()
        c.execute("UPDATE smtp_presets SET sent_today=sent_today+1 WHERE id=?", (smtp_id,))
        c.commit()

    def get_smtp_remaining(self, smtp_id: int) -> int:
        r = self._conn().execute("SELECT daily_limit, sent_today FROM smtp_presets WHERE id=?",
                                  (smtp_id,)).fetchone()
        if not r or r["daily_limit"] == 0:
            return 999999
        return max(0, r["daily_limit"] - r["sent_today"])

    # --- Lead Lists ---
    def create_list(self, name: str, file_origin: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO lead_lists (name, file_origin) VALUES (?,?)", (name, file_origin))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def import_leads(self, list_id: int, emails: List[str], exclude_domains: List[str] = None) -> int:
        c = self._conn()
        added = 0
        batch = []
        for email in emails:
            email = email.strip().lower()
            if not email or "@" not in email:
                continue
            if exclude_domains:
                domain = email.split("@")[1]
                if domain in exclude_domains:
                    continue
            batch.append((list_id, email))
            if len(batch) >= 500:
                c.executemany("INSERT INTO leads (list_id, email) VALUES (?,?)", batch)
                added += len(batch)
                batch = []
        if batch:
            c.executemany("INSERT INTO leads (list_id, email) VALUES (?,?)", batch)
            added += len(batch)
        c.execute("UPDATE lead_lists SET lead_count=? WHERE id=?", (added, list_id))
        c.commit()
        return added

    def get_lists(self) -> list:
        return self._conn().execute("SELECT * FROM lead_lists ORDER BY name").fetchall()

    def delete_list(self, list_id: int):
        c = self._conn()
        c.execute("DELETE FROM lead_lists WHERE id=?", (list_id,))
        c.commit()

    def search_leads(self, list_id: int, query: str) -> list:
        return self._conn().execute(
            "SELECT * FROM leads WHERE list_id=? AND email LIKE ? LIMIT 500",
            (list_id, f"%{query}%")).fetchall()

    def delete_leads_by_domain(self, list_id: int, domain: str) -> int:
        c = self._conn()
        c.execute("DELETE FROM leads WHERE list_id=? AND email LIKE ?",
                  (list_id, f"%@{domain}"))
        deleted = c.execute("SELECT changes()").fetchone()[0]
        c.execute("UPDATE lead_lists SET lead_count=(SELECT COUNT(*) FROM leads WHERE list_id=?) WHERE id=?",
                  (list_id, list_id))
        c.commit()
        return deleted

    def delete_leads_by_ids(self, lead_ids: List[int]):
        c = self._conn()
        ph = ",".join("?" for _ in lead_ids)
        c.execute(f"DELETE FROM leads WHERE id IN ({ph})", lead_ids)
        c.commit()

    def get_list_lead_count(self, list_id: int) -> int:
        r = self._conn().execute("SELECT COUNT(*) FROM leads WHERE list_id=?", (list_id,)).fetchone()
        return r[0] if r else 0

    def fetch_pending(self, list_id: int, exclude_domains: List[str] = None,
                      batch_size: int = 200) -> List[Tuple[int, str]]:
        c = self._conn()
        rows = c.execute(
            "SELECT id, email FROM leads WHERE list_id=? AND state='PENDING' ORDER BY id LIMIT ?",
            (list_id, batch_size)).fetchall()
        if not exclude_domains:
            return [(r["id"], r["email"]) for r in rows]
        return [(r["id"], r["email"]) for r in rows
                if r["email"].split("@")[1] not in exclude_domains]

    def mark_sent(self, lead_id: int):
        c = self._conn()
        c.execute("UPDATE leads SET state='SENT', updated_at=CURRENT_TIMESTAMP WHERE id=?", (lead_id,))
        c.commit()

    def mark_failed(self, lead_id: int, error: str = ""):
        c = self._conn()
        c.execute("UPDATE leads SET state='FAILED', error_msg=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (error[:500], lead_id))
        c.commit()

    def mark_excluded(self, lead_id: int):
        c = self._conn()
        c.execute("UPDATE leads SET state='EXCLUDED', updated_at=CURRENT_TIMESTAMP WHERE id=?", (lead_id,))
        c.commit()

    def mark_in_progress(self, lead_ids: List[int]):
        if not lead_ids:
            return
        c = self._conn()
        ph = ",".join("?" for _ in lead_ids)
        c.execute(f"UPDATE leads SET state='IN_PROGRESS', updated_at=CURRENT_TIMESTAMP WHERE id IN ({ph})", lead_ids)
        c.commit()

    def reset_in_progress(self, list_id: int = None):
        c = self._conn()
        if list_id:
            c.execute("UPDATE leads SET state='PENDING' WHERE state='IN_PROGRESS' AND list_id=?", (list_id,))
        else:
            c.execute("UPDATE leads SET state='PENDING' WHERE state='IN_PROGRESS'")
        c.commit()

    def mailing_stats(self, list_id: int) -> dict:
        c = self._conn()
        rows = c.execute("SELECT state, COUNT(*) FROM leads WHERE list_id=? GROUP BY state",
                         (list_id,)).fetchall()
        stats = {"PENDING": 0, "SENT": 0, "FAILED": 0, "EXCLUDED": 0, "IN_PROGRESS": 0, "total": 0}
        for r in rows:
            stats[r[0]] = r[1]
            stats["total"] += r[1]
        return stats

    # --- Brand List Usage ---
    def mark_list_used(self, brand_id: int, list_id: int, mailing_id: int = None):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO brand_list_usage (brand_id, list_id, mailing_id) VALUES (?,?,?)",
                  (brand_id, list_id, mailing_id))
        c.commit()

    def get_used_lists(self, brand_id: int) -> list:
        return self._conn().execute(
            "SELECT l.* FROM lead_lists l JOIN brand_list_usage u ON l.id=u.list_id WHERE u.brand_id=?",
            (brand_id,)).fetchall()

    def get_unused_lists(self, brand_id: int) -> list:
        return self._conn().execute(
            "SELECT * FROM lead_lists WHERE id NOT IN "
            "(SELECT list_id FROM brand_list_usage WHERE brand_id=?)", (brand_id,)).fetchall()

    # --- Macros ---
    def add_macro(self, name: str, values: list) -> int:
        c = self._conn()
        c.execute("INSERT INTO macros (name, values_json) VALUES (?,?)",
                  (name, json.dumps(values, ensure_ascii=False)))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_macros(self) -> list:
        return self._conn().execute("SELECT * FROM macros ORDER BY name").fetchall()

    def get_macro_values(self, name: str) -> list:
        r = self._conn().execute("SELECT values_json FROM macros WHERE name=?", (name,)).fetchone()
        return json.loads(r["values_json"]) if r else []

    def update_macro(self, macro_id: int, values: list):
        c = self._conn()
        c.execute("UPDATE macros SET values_json=? WHERE id=?",
                  (json.dumps(values, ensure_ascii=False), macro_id))
        c.commit()

    def delete_macro(self, macro_id: int):
        c = self._conn()
        c.execute("DELETE FROM macros WHERE id=?", (macro_id,))
        c.commit()

    def export_macros(self) -> str:
        rows = self.get_macros()
        data = {r["name"]: json.loads(r["values_json"]) for r in rows}
        return json.dumps(data, indent=2, ensure_ascii=False)

    def import_macros(self, json_str: str) -> int:
        data = json.loads(json_str)
        c = self._conn()
        added = 0
        for name, values in data.items():
            c.execute("INSERT OR REPLACE INTO macros (name, values_json) VALUES (?,?)",
                      (name, json.dumps(values, ensure_ascii=False)))
            added += 1
        c.commit()
        return added

    # --- Message Templates ---
    def add_template(self, name: str, **kw) -> int:
        c = self._conn()
        cols = ["name"] + [k for k in kw if k in (
            "html_files_json", "html_rotate_every", "pdf_path", "pdf_macro_enabled",
            "subject_macro", "sender_rotate_json", "sender_rotate_every", "settings_json")]
        vals = [name] + [kw[k] for k in cols[1:]]
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO message_templates ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_templates(self) -> list:
        return self._conn().execute("SELECT * FROM message_templates ORDER BY name").fetchall()

    def delete_template(self, template_id: int):
        c = self._conn()
        c.execute("DELETE FROM message_templates WHERE id=?", (template_id,))
        c.commit()

    # --- Mailings ---
    def create_mailing(self, **kw) -> int:
        c = self._conn()
        cols = [k for k in kw]
        vals = [kw[k] for k in cols]
        ph = ",".join("?" for _ in vals)
        c.execute(f"INSERT INTO mailings ({','.join(cols)}) VALUES ({ph})", vals)
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_mailings(self) -> list:
        return self._conn().execute("SELECT * FROM mailings ORDER BY created_at DESC").fetchall()

    def update_mailing_status(self, mailing_id: int, status: str):
        c = self._conn()
        ts_col = {"RUNNING": "started_at", "PAUSED": "paused_at", "FINISHED": "finished_at"}.get(status)
        if ts_col:
            c.execute(f"UPDATE mailings SET status=?, {ts_col}=CURRENT_TIMESTAMP WHERE id=?",
                      (status, mailing_id))
        else:
            c.execute("UPDATE mailings SET status=? WHERE id=?", (status, mailing_id))
        c.commit()

    def update_mailing_counts(self, mailing_id: int, sent: int, failed: int, excluded: int):
        c = self._conn()
        c.execute("UPDATE mailings SET sent=?, failed=?, excluded=? WHERE id=?",
                  (sent, failed, excluded, mailing_id))
        c.commit()

    # --- Backup ---
    def export_all(self) -> str:
        data = {
            "brands": [dict(r) for r in self.get_brands()],
            "domains": [dict(r) for r in self.get_domains()],
            "smtp_presets": [dict(r) for r in self.get_smtps()],
            "macros": {r["name"]: json.loads(r["values_json"]) for r in self.get_macros()},
            "templates": [dict(r) for r in self.get_templates()],
            "lead_lists": [dict(r) for r in self.get_lists()],
        }
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    # --- Cloudflare Accounts ---
    def add_cf_account(self, name: str, auth_type: str = "token",
                       api_token: str = "", global_api_key: str = "",
                       auth_email: str = "", account_id: str = "",
                       r2_access_key: str = "", r2_secret_key: str = "") -> int:
        c = self._conn()
        c.execute("INSERT INTO cf_accounts (name,auth_type,api_token,global_api_key,"
                  "auth_email,account_id,r2_access_key,r2_secret_key) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (name, auth_type, api_token, global_api_key, auth_email,
                   account_id, r2_access_key, r2_secret_key))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_cf_accounts(self) -> list:
        return self._conn().execute("SELECT * FROM cf_accounts ORDER BY name").fetchall()

    def delete_cf_account(self, cf_id: int):
        c = self._conn()
        c.execute("DELETE FROM cf_accounts WHERE id=?", (cf_id,))
        c.commit()

    def update_cf_account(self, cf_id: int, **kw):
        c = self._conn()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE cf_accounts SET {sets} WHERE id=?", list(kw.values()) + [cf_id])
        c.commit()

    # --- Domain Unsub Status ---
    def mark_unsub_deployed(self, domain_id: int, unsub_domain: str):
        c = self._conn()
        c.execute("UPDATE domains SET unsub_worker_deployed=1, unsub_domain=? WHERE id=?",
                  (unsub_domain, domain_id))
        c.commit()

    def is_unsub_deployed(self, domain_id: int) -> bool:
        r = self._conn().execute("SELECT unsub_worker_deployed FROM domains WHERE id=?",
                                  (domain_id,)).fetchone()
        return bool(r and r[0])

    def get_unsub_domain(self, domain_id: int) -> str:
        r = self._conn().execute("SELECT unsub_domain FROM domains WHERE id=?",
                                  (domain_id,)).fetchone()
        return r["unsub_domain"] if r and r["unsub_domain"] else ""

    # --- List-ID Generation ---
    @staticmethod
    def generate_list_id(domain: str, campaign_id: int = 0) -> str:
        import secrets
        short = secrets.token_hex(4)
        return f"c{campaign_id}-{short}.{domain}"

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
