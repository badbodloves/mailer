"""Bulk Mailer Core.

Orchestrates bulk/newsletter sending with rate limiting, macro processing,
PDF obfuscation, sender/subject rotation, and domain excludes.
"""
import os
import json
import time
import threading
import logging
from email.utils import formatdate
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from .db_manager import BulkDBManager
from .bulk_mime_builder import BulkMIMEBuilder
from .content_engine import BulkContentEngine
from .rate_limiter import RateLimiter
from .smtp_client import SMTPClient
from .pdf_macro import fill_pdf_macro

logger = logging.getLogger("bulk.core")


class BulkMailerCore:
    BATCH_SIZE = 200

    def __init__(self, db: BulkDBManager, mailing_id: int):
        self._db = db
        self._mailing_id = mailing_id
        self._shutdown = threading.Event()
        self._send_count = 0
        self._lock = threading.Lock()

    def stop(self):
        self._shutdown.set()

    def run(self):
        mailing = self._load_mailing()
        if not mailing:
            logger.error("Mailing %d not found", self._mailing_id)
            return

        domain_row = self._db._conn().execute(
            "SELECT * FROM domains WHERE id=?", (mailing["domain_id"],)).fetchone()
        smtp_row = self._db._conn().execute(
            "SELECT * FROM smtp_presets WHERE id=?", (mailing["smtp_preset_id"],)).fetchone()
        template_row = self._db._conn().execute(
            "SELECT * FROM message_templates WHERE id=?", (mailing["template_id"],)).fetchone()

        if not all([domain_row, smtp_row, template_row]):
            logger.error("Missing domain, SMTP, or template for mailing %d", self._mailing_id)
            return

        macros = {r["name"]: json.loads(r["values_json"]) for r in self._db.get_macros()}
        engine = BulkContentEngine(macros)

        smtp = SMTPClient(smtp_row["host"], smtp_row["port"],
                          smtp_row["username"], smtp_row["password"])

        daily_limit = mailing["daily_limit"] or smtp_row["daily_limit"] or 0
        limiter = RateLimiter(daily_limit=daily_limit)

        domain = domain_row["domain"]
        from_email = domain_row["from_email"] or f"newsletter@{domain}"
        reply_to = domain_row["reply_to"] or from_email
        list_id_label = domain_row["list_id_label"] or f"newsletter.{domain}"
        list_id = f'"{list_id_label}" <{list_id_label}>'
        bounce_sub = domain_row["bounce_subdomain"] or "bounce"

        html_files = json.loads(template_row["html_files_json"] or "[]")
        html_rotate = template_row["html_rotate_every"] or 0
        sender_list = json.loads(template_row["sender_rotate_json"] or "[]")
        sender_rotate = template_row["sender_rotate_every"] or 0
        subject_macro = template_row["subject_macro"] or ""
        pdf_path = template_row["pdf_path"] or ""
        pdf_macro_on = bool(template_row["pdf_macro_enabled"])

        pdf_bytes = None
        if pdf_path and os.path.isfile(pdf_path):
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

        html_contents = []
        for hf in html_files:
            if os.path.isfile(hf):
                with open(hf, "r", encoding="utf-8") as f:
                    html_contents.append(f.read())

        exclude_domains = json.loads(mailing["exclude_domains_json"] or "[]")
        list_id_db = mailing["list_id"]

        feedback_base = f"newsletter:{domain}:m{self._mailing_id}"

        self._db.update_mailing_status(self._mailing_id, "RUNNING")
        self._db.reset_in_progress(list_id_db)
        self._db.reset_daily_counts()

        sent = 0
        failed = 0
        excluded = 0

        while not self._shutdown.is_set():
            batch = self._db.fetch_pending(list_id_db, exclude_domains, self.BATCH_SIZE)
            if not batch:
                break

            lead_ids = [r[0] for r in batch]
            self._db.mark_in_progress(lead_ids)

            for lead_id, email in batch:
                if self._shutdown.is_set():
                    break

                remaining = self._db.get_smtp_remaining(smtp_row["id"])
                if remaining <= 0:
                    logger.warning("SMTP daily limit reached")
                    self._shutdown.set()
                    break

                limiter.wait()

                with self._lock:
                    idx = self._send_count

                cur_from_name = domain_row["from_name"] or "Newsletter"
                if sender_list:
                    cur_from_name = engine.get_rotated_value(sender_list, idx, sender_rotate)

                cur_subject = subject_macro
                cur_subject = engine.process(cur_subject, email)

                html_template = ""
                if html_contents:
                    html_idx = engine.get_rotated_value(
                        list(range(len(html_contents))), idx, html_rotate) if html_rotate else 0
                    html_template = html_contents[int(html_idx)] if isinstance(html_idx, (int, float)) else html_contents[0]
                else:
                    html_template = "<p>Hello {email_user}</p>"

                html_body = engine.process(html_template, email)
                plain_body = engine.html_to_plaintext(html_body)

                unsub_token = f"{lead_id}-{self._mailing_id}"
                unsub_url = f"https://unsub.{domain}/u/{unsub_token}"
                unsub_mailto = f"unsub-{unsub_token}@{domain}"

                feedback_id = f"{feedback_base}:{domain.replace('.', '-')[:15]}"

                attachment = None
                if pdf_bytes:
                    if pdf_macro_on:
                        mod_pdf = fill_pdf_macro(pdf_bytes)
                        attachment = (os.path.basename(pdf_path), mod_pdf)
                    else:
                        attachment = (os.path.basename(pdf_path), pdf_bytes)

                try:
                    raw_msg, envelope_from, verp_tag = BulkMIMEBuilder.build_email(
                        from_name=engine.process(cur_from_name, email),
                        from_email=from_email,
                        reply_to_name="",
                        reply_to_email=reply_to,
                        to_email=email,
                        subject=cur_subject,
                        html_body=html_body,
                        plain_body=plain_body,
                        list_id_token=list_id_label,
                        list_id_name=list_id_label.split(".")[0].title() if list_id_label else "",
                        unsubscribe_url=unsub_url,
                        unsubscribe_mailto=unsub_mailto,
                        feedback_id=feedback_id,
                        bounce_domain=f"{bounce_sub}.{domain}",
                        recipient_id=str(lead_id),
                        attachment=attachment,
                        provider_type=smtp_row.get("provider_type", "generic"),
                    )

                    date_line = f"Date: {formatdate(localtime=True)}\r\n"
                    raw_msg = date_line + raw_msg

                    success, error, code = smtp.send(envelope_from, email, raw_msg)

                    if success:
                        self._db.mark_sent(lead_id)
                        self._db.increment_smtp_sent(smtp_row["id"])
                        sent += 1
                    elif error.startswith("FATAL:"):
                        self._db.mark_failed(lead_id, error)
                        failed += 1
                    else:
                        self._db._conn().execute(
                            "UPDATE leads SET state='PENDING' WHERE id=?", (lead_id,))
                        self._db._conn().commit()
                        failed += 1

                except Exception as exc:
                    self._db.mark_failed(lead_id, str(exc)[:500])
                    failed += 1
                    logger.error("Send error: %s", exc)

                with self._lock:
                    self._send_count += 1

                self._db.update_mailing_counts(self._mailing_id, sent, failed, excluded)

        status = "FINISHED" if not self._shutdown.is_set() else "PAUSED"
        self._db.update_mailing_status(self._mailing_id, status)
        self._db.reset_in_progress(list_id_db)

        if mailing.get("brand_id"):
            self._db.mark_list_used(mailing["brand_id"], list_id_db, self._mailing_id)

    def _load_mailing(self):
        return self._db._conn().execute(
            "SELECT * FROM mailings WHERE id=?", (self._mailing_id,)).fetchone()
