import os
import sqlite3
import configparser
import tempfile
import webbrowser

CONFIG_PATH = "config.ini"
LOG_FILE = "smtp_errors.log"


def read_config() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if os.path.isfile(CONFIG_PATH):
        cp.read(CONFIG_PATH, encoding="utf-8")
    return cp


def save_config(cp: configparser.ConfigParser):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)


def db_path() -> str:
    cp = read_config()
    return cp.get("database", "db_path", fallback="mailer.db")


def db_stats() -> dict:
    path = db_path()
    r = {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0, "total": 0}
    if not os.path.isfile(path):
        return r
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        for s, n in conn.execute("SELECT state, COUNT(*) FROM leads GROUP BY state"):
            r[s] = n
        r["total"] = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        conn.close()
    except Exception:
        pass
    return r


def log_tail(n: int = 80) -> str:
    if not os.path.isfile(LOG_FILE):
        return "(no log file)"
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:]) or "(empty)"
    except OSError:
        return "(error)"


def scan_files(folder: str, exts: tuple = (".txt",)) -> list:
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(exts))


def open_html_in_browser(html: str, title: str = "Preview"):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
        webbrowser.open(f"file://{f.name}")


def open_raw_in_browser(raw: str):
    html = f"<html><body><pre style='font-family:monospace;font-size:13px;white-space:pre-wrap'>{_escape(raw)}</pre></body></html>"
    open_html_in_browser(html, "Raw MIME")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
