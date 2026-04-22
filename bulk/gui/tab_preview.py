"""HTML Preview + Raw MIME + Text:Image Ratio + Provider selection."""
import os
import json
import tempfile
import webbrowser
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QComboBox, QPushButton, QLabel, QTabWidget,
                                 QTextEdit, QMessageBox, QFormLayout, QLineEdit)
from PySide6.QtCore import Qt


class PreviewTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)

        sel = QGroupBox("Preview Settings")
        form = QFormLayout(sel)
        self.tmpl_cb = QComboBox()
        form.addRow("Template:", self.tmpl_cb)
        self.domain_cb = QComboBox()
        form.addRow("Domain:", self.domain_cb)
        self.provider_cb = QComboBox()
        self.provider_cb.addItems(["generic", "ses", "sendgrid", "mailgun", "postmark"])
        form.addRow("SMTP Provider:", self.provider_cb)
        self.test_email = QLineEdit("preview@example.com")
        form.addRow("Preview Email:", self.test_email)
        layout.addWidget(sel)

        btns = QHBoxLayout()
        for text, fn in [("Refresh", self._refresh), ("Preview HTML", self._preview_html),
                         ("Raw MIME", self._preview_mime), ("Text:Image Ratio", self._ratio)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        layout.addLayout(btns)

        tabs = QTabWidget()
        self.html_view = QTextEdit()
        self.html_view.setReadOnly(True)
        tabs.addTab(self.html_view, "HTML Source")
        self.mime_view = QTextEdit()
        self.mime_view.setReadOnly(True)
        self.mime_view.setStyleSheet("font-family:Consolas; background:#1e1e1e; color:#d4d4d4")
        tabs.addTab(self.mime_view, "Raw MIME")
        self.info_view = QTextEdit()
        self.info_view.setReadOnly(True)
        tabs.addTab(self.info_view, "Info")
        layout.addWidget(tabs)

        self._refresh()

    def _refresh(self):
        self.tmpl_cb.blockSignals(True)
        self.tmpl_cb.clear()
        for t in self.db.get_templates():
            self.tmpl_cb.addItem(t["name"], t["id"])
        self.tmpl_cb.blockSignals(False)

        self.domain_cb.blockSignals(True)
        self.domain_cb.clear()
        for d in self.db.get_domains():
            self.domain_cb.addItem(d["domain"], d["id"])
        self.domain_cb.blockSignals(False)

    def _build_preview(self):
        tid = self.tmpl_cb.currentData()
        did = self.domain_cb.currentData()
        if not tid or not did:
            QMessageBox.warning(self, "Select", "Select template and domain. Add them in Composer/Brands tabs first.")
            return None, None

        tmpl = dict(self.db._conn().execute("SELECT * FROM message_templates WHERE id=?", (tid,)).fetchone())
        domain = dict(self.db._conn().execute("SELECT * FROM domains WHERE id=?", (did,)).fetchone())

        from bulk.mailer.content_engine import BulkContentEngine
        from bulk.mailer.bulk_mime_builder import BulkMIMEBuilder
        from email.utils import formatdate

        macros = {r["name"]: json.loads(r["values_json"]) for r in self.db.get_macros()}
        engine = BulkContentEngine(macros)
        email = self.test_email.text()

        html_files = json.loads(tmpl.get("html_files_json") or "[]")
        html_body = "<p>No HTML template selected</p>"
        if html_files and os.path.isfile(html_files[0]):
            with open(html_files[0], "r", encoding="utf-8") as f:
                html_body = f.read()

        html_body = engine.process(html_body, email)
        subject = engine.process(tmpl.get("subject_macro") or "Preview", email)
        plain = engine.html_to_plaintext(html_body)
        from_email = domain.get("from_email") or f"newsletter@{domain['domain']}"
        from_name = domain.get("from_name") or "Newsletter"
        reply_to = domain.get("reply_to_email") or from_email
        provider = self.provider_cb.currentText()
        dom_name = domain["domain"]
        list_id_token = f"nl.{dom_name}"

        raw, envelope, tag = BulkMIMEBuilder.build_email(
            from_name=from_name, from_email=from_email,
            reply_to_name="", reply_to_email=reply_to,
            to_email=email, subject=subject, html_body=html_body, plain_body=plain,
            list_id_token=list_id_token, list_id_name="Newsletter",
            unsubscribe_url=f"https://unsub.{dom_name}/u/preview",
            unsubscribe_mailto=f"unsub-preview@{dom_name}",
            feedback_id=f"preview:test:p1:{dom_name.replace('.', '-')[:15]}",
            provider_type=provider,
        )

        date_line = f"Date: {formatdate(localtime=True)}\r\n"
        raw = date_line + raw

        return html_body, raw

    def _preview_html(self):
        html, _ = self._build_preview()
        if not html:
            return
        self.html_view.setPlainText(html)
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(html)
            webbrowser.open(f"file://{f.name}")

    def _preview_mime(self):
        _, raw = self._build_preview()
        if not raw:
            return
        self.mime_view.setPlainText(raw[:15000])

    def _ratio(self):
        html, raw = self._build_preview()
        if not html or not raw:
            return
        from bulk.mailer.content_engine import BulkContentEngine
        plain = BulkContentEngine.html_to_plaintext(html)
        text_bytes = len(plain.encode("utf-8"))
        total = len(raw.encode("utf-8"))
        text_pct = text_bytes / total * 100 if total else 0

        info = (f"Plain text: {text_bytes:,} bytes\n"
                f"Total MIME: {total:,} bytes\n"
                f"Text ratio: {text_pct:.0f}%\n\n")
        if text_pct >= 60:
            info += "GOOD — text-heavy"
        elif text_pct >= 30:
            info += "OK — balanced"
        else:
            info += "WARNING — too much non-text"
        self.info_view.setPlainText(info)
