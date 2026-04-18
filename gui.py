#!/usr/bin/env python3
"""Streamlit Web-GUI for the Mass Mailer system.

Run:  streamlit run gui.py
"""

import os
import sys
import time
import glob
import sqlite3
import configparser
import threading
from datetime import datetime, timedelta

import streamlit as st
import psutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mailer.mailer_core import MailerCore
from mailer.db_manager import DBManager
from mailer.content_engine import ContentEngine

st.set_page_config(page_title="Mass Mailer", page_icon="📧", layout="wide")

CONFIG_PATH = "config.ini"
LEADS_DIR = "Leads"
SMTPS_DIR = "SMTPs"
HTML_DIR = "html_bodies"
LOG_FILE = "smtp_errors.log"


def _ensure_dirs() -> None:
    for d in (LEADS_DIR, SMTPS_DIR, HTML_DIR):
        os.makedirs(d, exist_ok=True)


def _scan_txt(folder: str) -> list:
    if not os.path.isdir(folder):
        return []
    return sorted(
        f for f in os.listdir(folder) if f.lower().endswith(".txt")
    )


def _read_config() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if os.path.isfile(CONFIG_PATH):
        cp.read(CONFIG_PATH, encoding="utf-8")
    return cp


def _write_config(cp: configparser.ConfigParser) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        cp.write(fh)


def _db_stats(db_path: str) -> dict:
    if not os.path.isfile(db_path):
        return {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0, "total": 0}
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute("SELECT state, COUNT(*) FROM leads GROUP BY state").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        conn.close()
        counts = {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0, "total": total}
        for state, cnt in rows:
            counts[state] = cnt
        return counts
    except Exception:
        return {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0, "total": 0}


def _smtp_accounts_from_file(path: str) -> list:
    accounts = []
    if not os.path.isfile(path):
        return accounts
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) >= 4:
                    accounts.append({
                        "host": parts[0].strip(),
                        "port": parts[1].strip(),
                        "user": parts[2].strip(),
                    })
    except OSError:
        pass
    return accounts


def _read_log_tail(n: int = 50) -> str:
    if not os.path.isfile(LOG_FILE):
        return "(no log file)"
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-n:]) if lines else "(empty)"
    except OSError:
        return "(read error)"


def _mailer_thread(overrides: dict) -> None:
    try:
        core = MailerCore(config_path=CONFIG_PATH, overrides=overrides)
        st.session_state["_core"] = core
        core.run()
    except Exception as exc:
        st.session_state["mailer_error"] = str(exc)
    finally:
        st.session_state["running"] = False


def _start_mailer(overrides: dict) -> None:
    st.session_state["running"] = True
    st.session_state["started_at"] = time.time()
    st.session_state["mailer_error"] = ""
    t = threading.Thread(target=_mailer_thread, args=(overrides,), daemon=True)
    t.start()
    st.session_state["_thread"] = t


def _stop_mailer() -> None:
    core = st.session_state.get("_core")
    if core:
        core.stop()


_ensure_dirs()

if "running" not in st.session_state:
    st.session_state["running"] = False
if "started_at" not in st.session_state:
    st.session_state["started_at"] = 0
if "mailer_error" not in st.session_state:
    st.session_state["mailer_error"] = ""
if "scheduler_time" not in st.session_state:
    st.session_state["scheduler_time"] = None
if "html_preview" not in st.session_state:
    st.session_state["html_preview"] = ""
if "log_auto" not in st.session_state:
    st.session_state["log_auto"] = False

st.title("📧 Mass Mailer Control Panel")

tab_main, tab_editor, tab_config, tab_logs = st.tabs([
    "🚀 Campaign", "📝 HTML Editor", "⚙️ Config", "📋 Logs"
])

cp = _read_config()
db_path = cp.get("database", "db_path", fallback="mailer.db")

with tab_main:
    col_ctrl, col_stats = st.columns([1, 2])

    with col_ctrl:
        st.subheader("File Selection")

        leads_files = _scan_txt(LEADS_DIR)
        smtp_files = _scan_txt(SMTPS_DIR)

        sel_leads = st.selectbox(
            "Lead List",
            leads_files if leads_files else ["(no files in Leads/)"],
            disabled=st.session_state["running"],
        )
        sel_smtp = st.selectbox(
            "SMTP Pool",
            smtp_files if smtp_files else ["(no files in SMTPs/)"],
            disabled=st.session_state["running"],
        )

        st.divider()
        st.subheader("Upload Files")
        up_type = st.radio("Upload to", ["Leads/", "SMTPs/"], horizontal=True)
        uploaded = st.file_uploader("Select .txt file", type=["txt"], key="upload")
        if uploaded:
            target_dir = LEADS_DIR if up_type == "Leads/" else SMTPS_DIR
            dest = os.path.join(target_dir, uploaded.name)
            with open(dest, "wb") as fh:
                fh.write(uploaded.getvalue())
            st.success(f"Saved to {dest}")
            st.rerun()

        st.divider()
        st.subheader("Control")

        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "▶ START",
                type="primary",
                use_container_width=True,
                disabled=st.session_state["running"] or not leads_files or not smtp_files,
            ):
                overrides = {
                    "paths.leads_file": os.path.join(LEADS_DIR, sel_leads),
                    "paths.smtp_file": os.path.join(SMTPS_DIR, sel_smtp),
                }
                _start_mailer(overrides)
                st.rerun()

        with c2:
            if st.button(
                "⏹ STOP",
                type="secondary",
                use_container_width=True,
                disabled=not st.session_state["running"],
            ):
                _stop_mailer()
                st.info("Stop signal sent. Finishing current batch...")

        if st.session_state["mailer_error"]:
            st.error(st.session_state["mailer_error"])

        st.divider()
        st.subheader("Scheduler")
        sched_date = st.date_input("Date", value=datetime.now().date())
        sched_time = st.time_input("Time", value=datetime.now().time())
        if st.button("Schedule Campaign", disabled=st.session_state["running"] or not leads_files or not smtp_files):
            target_dt = datetime.combine(sched_date, sched_time)
            delay = (target_dt - datetime.now()).total_seconds()
            if delay > 0:
                st.session_state["scheduler_time"] = target_dt
                st.success(f"Scheduled for {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (in {int(delay)}s)")

                def _scheduled_start():
                    time.sleep(delay)
                    if not st.session_state["running"]:
                        overrides = {
                            "paths.leads_file": os.path.join(LEADS_DIR, sel_leads),
                            "paths.smtp_file": os.path.join(SMTPS_DIR, sel_smtp),
                        }
                        _start_mailer(overrides)

                threading.Thread(target=_scheduled_start, daemon=True).start()
            else:
                st.warning("Time is in the past.")

        if st.session_state["scheduler_time"]:
            remaining = (st.session_state["scheduler_time"] - datetime.now()).total_seconds()
            if remaining > 0:
                st.info(f"⏰ Starts in {int(remaining // 60)}m {int(remaining % 60)}s")
            else:
                st.session_state["scheduler_time"] = None

    with col_stats:
        st.subheader("Live Dashboard")

        stats = _db_stats(db_path)
        total = stats["total"]
        sent = stats["SENT"]
        failed = stats["FAILED"]
        pending = stats["PENDING"]
        in_prog = stats["IN_PROGRESS"]
        processed = sent + failed

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total", total)
        m2.metric("Sent", sent)
        m3.metric("Failed", failed)
        m4.metric("Pending", pending + in_prog)

        progress = processed / total if total > 0 else 0
        st.progress(progress, text=f"{progress * 100:.1f}%")

        elapsed = time.time() - st.session_state["started_at"] if st.session_state["running"] else 0
        speed = processed / elapsed if elapsed > 0 else 0
        remaining = (total - processed) / speed if speed > 0 else 0
        eta_str = str(timedelta(seconds=int(remaining))) if speed > 0 else "--:--:--"

        s1, s2, s3 = st.columns(3)
        s1.metric("Speed", f"{speed:.1f} m/s")
        s2.metric("ETA", eta_str)
        s3.metric("Status", "🟢 Running" if st.session_state["running"] else "⚪ Idle")

        st.divider()

        if sent + failed > 0:
            import plotly.graph_objects as go
            fig = go.Figure(data=[go.Pie(
                labels=["Sent", "Failed"],
                values=[sent, failed],
                marker=dict(colors=["#00cc66", "#ff4444"]),
                hole=0.4,
            )])
            fig.update_layout(height=250, margin=dict(t=20, b=20, l=20, r=20))
            st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("System Monitor")
        sys1, sys2 = st.columns(2)
        sys1.metric("CPU", f"{psutil.cpu_percent(interval=0):.0f}%")
        sys2.metric("RAM", f"{psutil.virtual_memory().percent:.0f}%")

        st.divider()
        st.subheader("SMTP Accounts")
        smtp_path = os.path.join(SMTPS_DIR, sel_smtp) if smtp_files else ""
        accounts = _smtp_accounts_from_file(smtp_path)
        if accounts:
            st.dataframe(
                accounts,
                use_container_width=True,
                column_config={"host": "Host", "port": "Port", "user": "User"},
            )
        else:
            st.info("No SMTP accounts loaded.")

    if st.session_state["running"]:
        time.sleep(2)
        st.rerun()

with tab_editor:
    st.subheader("HTML Body Editor")

    html_files = []
    if os.path.isdir(HTML_DIR):
        html_files = sorted(
            f for f in os.listdir(HTML_DIR) if f.lower().endswith((".html", ".htm"))
        )

    selected_html = st.selectbox("Template file", ["(new)"] + html_files)

    existing_content = ""
    if selected_html != "(new)" and selected_html:
        try:
            with open(os.path.join(HTML_DIR, selected_html), "r", encoding="utf-8") as fh:
                existing_content = fh.read()
        except OSError:
            pass

    html_source = st.text_area("HTML Source", value=existing_content, height=400, key="html_edit")

    ec1, ec2, ec3 = st.columns(3)
    with ec1:
        save_name = st.text_input("Filename", value=selected_html if selected_html != "(new)" else "template.html")
    with ec2:
        if st.button("💾 Save"):
            path = os.path.join(HTML_DIR, save_name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html_source)
            st.success(f"Saved to {path}")
    with ec3:
        if st.button("👁 Preview"):
            try:
                ce = ContentEngine(
                    html_dir=HTML_DIR,
                    attachments_dir="",
                    spintax_dir=cp.get("paths", "spintax_dir", fallback="spintaxes"),
                    names_file=cp.get("paths", "names_file", fallback=""),
                    subjects_file=cp.get("paths", "subjects_file", fallback=""),
                )
                rendered = ce.process(html_source, "preview@example.com")
                st.session_state["html_preview"] = rendered
            except Exception as exc:
                st.error(f"Preview error: {exc}")

    if st.session_state["html_preview"]:
        st.divider()
        st.subheader("Rendered Preview")
        st.components.v1.html(st.session_state["html_preview"], height=600, scrolling=True)

with tab_config:
    st.subheader("Configuration Editor")

    cp_edit = _read_config()

    with st.form("config_form"):
        cfg1, cfg2 = st.columns(2)

        with cfg1:
            st.markdown("**Sending**")
            threads = st.number_input("Threads", 1, 200,
                                       cp_edit.getint("sending", "threads", fallback=40))
            normal_delay = st.number_input("Normal Delay (s)", 0.0, 60.0,
                                            cp_edit.getfloat("sending", "normal_delay", fallback=0.3),
                                            step=0.1)
            provider_delay = st.number_input("Provider Delay (s)", 0.0, 60.0,
                                              cp_edit.getfloat("sending", "provider_delay", fallback=6.0),
                                              step=0.5)
            warmup_delay = st.number_input("Warmup Delay (s)", 0.0, 120.0,
                                            cp_edit.getfloat("sending", "warmup_delay", fallback=30.0),
                                            step=5.0)
            ignore_ssl = st.checkbox("Ignore SSL Errors",
                                      cp_edit.get("sending", "ignore_ssl_errors", fallback="true").lower() in ("true", "1", "yes"))

        with cfg2:
            st.markdown("**Sender**")
            from_name = st.text_input("From Name", cp_edit.get("sender", "from_name", fallback="{from_name}"))
            from_email = st.text_input("From Email (empty = SMTP user)", cp_edit.get("sender", "from_email", fallback=""))
            subject = st.text_input("Subject", cp_edit.get("sender", "subject", fallback=""))

            st.markdown("**Test**")
            test_recip = st.text_input("Test Recipients (comma-sep)", cp_edit.get("test", "test_recipients", fallback=""))

        submitted = st.form_submit_button("💾 Save Config", type="primary")
        if submitted:
            cp_edit.set("sending", "threads", str(int(threads)))
            cp_edit.set("sending", "normal_delay", str(normal_delay))
            cp_edit.set("sending", "provider_delay", str(provider_delay))
            cp_edit.set("sending", "warmup_delay", str(warmup_delay))
            cp_edit.set("sending", "ignore_ssl_errors", str(ignore_ssl))
            cp_edit.set("sender", "from_name", from_name)
            cp_edit.set("sender", "from_email", from_email)
            cp_edit.set("sender", "subject", subject)
            cp_edit.set("test", "test_recipients", test_recip)
            _write_config(cp_edit)
            st.success("Config saved.")

    st.divider()
    st.subheader("Database Management")
    db1, db2 = st.columns(2)
    with db1:
        if st.button("🔄 Reset IN_PROGRESS to PENDING"):
            try:
                db = DBManager(db_path)
                db.reset_in_progress()
                db.close()
                st.success("Done.")
            except Exception as exc:
                st.error(str(exc))
    with db2:
        if st.button("🗑 Delete Database (full restart)", type="secondary"):
            if os.path.isfile(db_path):
                os.unlink(db_path)
                st.success("Database deleted. Next run starts fresh.")

with tab_logs:
    st.subheader("SMTP Error Log")
    auto_refresh = st.toggle("Auto-refresh (2s)", value=st.session_state["log_auto"], key="log_toggle")
    st.session_state["log_auto"] = auto_refresh
    log_text = _read_log_tail(50)
    st.code(log_text, language="text")

    if auto_refresh:
        time.sleep(2)
        st.rerun()
