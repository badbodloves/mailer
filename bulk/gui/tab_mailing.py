"""Mailing control — select brand/domain/list/smtp/template → Start."""
import json
import time
import threading
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QComboBox, QPushButton, QLabel, QSpinBox,
                                 QLineEdit, QProgressBar, QTextEdit,
                                 QMessageBox, QFormLayout, QSplitter)
from PySide6.QtCore import Qt, QTimer


class MailingTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._thread = None
        self._core = None
        self._running = False
        self._started_at = 0.0
        layout = QHBoxLayout(self)

        left = QWidget()
        ll = QVBoxLayout(left)

        setup = QGroupBox("Mailing Setup")
        form = QFormLayout(setup)

        self.brand_cb = QComboBox()
        self.brand_cb.currentIndexChanged.connect(self._on_brand_change)
        form.addRow("Brand:", self.brand_cb)

        self.domain_cb = QComboBox()
        form.addRow("Sender Domain:", self.domain_cb)

        sender_row = QHBoxLayout()
        self.sender_name_input = QLineEdit()
        self.sender_name_input.setPlaceholderText("Display name or {macro}")
        sender_row.addWidget(self.sender_name_input)
        insert_sender_btn = QPushButton("Insert Macro")
        insert_sender_btn.clicked.connect(self._insert_sender_macro)
        sender_row.addWidget(insert_sender_btn)
        form.addRow("Sender Name:", sender_row)

        self.list_cb = QComboBox()
        form.addRow("Mailing List:", self.list_cb)

        self.smtp_cb = QComboBox()
        form.addRow("SMTP Preset:", self.smtp_cb)

        self.tmpl_cb = QComboBox()
        form.addRow("Message:", self.tmpl_cb)

        self.daily_limit = QSpinBox()
        self.daily_limit.setRange(0, 999999)
        self.daily_limit.setSpecialValueText("Use SMTP limit")
        form.addRow("Daily Limit:", self.daily_limit)

        self.exclude_input = QLineEdit()
        self.exclude_input.setPlaceholderText("yahoo.de, aol.com")
        form.addRow("Exclude Domains:", self.exclude_input)

        ll.addWidget(setup)

        ctrl = QGroupBox("Control")
        cl = QHBoxLayout(ctrl)
        self.btn_start = QPushButton("▶ START")
        self.btn_start.clicked.connect(self._start)
        self.btn_pause = QPushButton("⏸ PAUSE")
        self.btn_pause.clicked.connect(self._pause)
        self.btn_pause.setEnabled(False)
        self.btn_stop = QPushButton("⏹ STOP")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._refresh_all)
        cl.addWidget(self.btn_start)
        cl.addWidget(self.btn_pause)
        cl.addWidget(self.btn_stop)
        cl.addWidget(self.btn_refresh)
        ll.addWidget(ctrl)

        stats = QGroupBox("Delivery Info")
        sl = QVBoxLayout(stats)
        self.status_label = QLabel("Status: Idle")
        self.status_label.setStyleSheet("font-weight:bold; font-size:14px")
        sl.addWidget(self.status_label)
        self.progress = QProgressBar()
        sl.addWidget(self.progress)

        metrics = QHBoxLayout()
        self._metric_labels = {}
        for key in ["Total", "Sent", "Failed", "Excluded", "Speed", "Elapsed", "ETA"]:
            metrics.addWidget(QLabel(f"{key}:"))
            lbl = QLabel("0")
            lbl.setStyleSheet("font-weight:bold")
            metrics.addWidget(lbl)
            self._metric_labels[key] = lbl
        sl.addLayout(metrics)
        ll.addWidget(stats)

        right = QGroupBox("Events Log")
        rl = QVBoxLayout(right)
        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.events.setStyleSheet("background:#1e1e1e; color:#d4d4d4; font-family:Consolas")
        rl.addWidget(self.events)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([600, 400])
        layout.addWidget(splitter)

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(2000)

        self._refresh_all()

    def _log(self, msg):
        self.events.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _refresh_all(self):
        self.brand_cb.blockSignals(True)
        self.brand_cb.clear()
        for b in self.db.get_brands():
            self.brand_cb.addItem(b["name"], b["id"])
        self.brand_cb.blockSignals(False)

        self.smtp_cb.clear()
        self.db.reset_daily_counts()
        for s in self.db.get_smtps():
            remaining = self.db.get_smtp_remaining(s["id"])
            proxy = " [proxy]" if s.get("proxy") else ""
            self.smtp_cb.addItem(f"{s['name']} ({remaining:,} left){proxy}", s["id"])

        self.tmpl_cb.clear()
        for t in self.db.get_templates():
            self.tmpl_cb.addItem(t["name"], t["id"])

        self.list_cb.clear()
        for l in self.db.get_lists():
            count = self.db.get_list_lead_count(l["id"])
            self.list_cb.addItem(f"{l['name']} ({count:,})", l["id"])

        if self.brand_cb.count() > 0:
            self.brand_cb.setCurrentIndex(0)
            self._on_brand_change()

    def _on_brand_change(self):
        self.domain_cb.clear()
        bid = self.brand_cb.currentData()
        if not bid:
            return
        for d in self.db.get_domains(bid):
            label = d["domain"]
            if d.get("from_email"):
                label += f" ({d['from_email']})"
            self.domain_cb.addItem(label, d["id"])

    def _insert_sender_macro(self):
        from .macro_insert import insert_macro_into
        insert_macro_into(self.db, self.sender_name_input, self)

    def _start(self):
        if self._running:
            return
        brand_id = self.brand_cb.currentData()
        domain_id = self.domain_cb.currentData()
        list_id = self.list_cb.currentData()
        smtp_id = self.smtp_cb.currentData()
        tmpl_id = self.tmpl_cb.currentData()

        missing = []
        if not brand_id: missing.append("Brand")
        if not domain_id: missing.append("Domain")
        if not list_id: missing.append("List")
        if not smtp_id: missing.append("SMTP")
        if not tmpl_id: missing.append("Template")
        if missing:
            QMessageBox.warning(self, "Missing", f"Select: {', '.join(missing)}")
            return

        excludes = [d.strip() for d in self.exclude_input.text().split(",") if d.strip()]

        mailing_id = self.db.create_mailing(
            brand_id=brand_id, domain_id=domain_id, list_id=list_id,
            smtp_preset_id=smtp_id, template_id=tmpl_id,
            daily_limit=self.daily_limit.value(),
            exclude_domains_json=json.dumps(excludes),
            total_leads=self.db.get_list_lead_count(list_id),
        )

        self._log(f"Mailing #{mailing_id} created")

        from bulk.mailer.bulk_core import BulkMailerCore
        self._core = BulkMailerCore(self.db, mailing_id)

        def run():
            try:
                self._core.run()
                self._log("Mailing finished")
            except Exception as e:
                self._log(f"ERROR: {e}")
            finally:
                self._running = False
                self._core = None

        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.status_label.setText("Status: Running")
        self._log("Mailing started")

    def _pause(self):
        if self._core:
            self._core.stop()
            self.status_label.setText("Status: Paused")
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self._log("Paused")

    def _stop(self):
        if self._core:
            self._core.stop()
        self._running = False
        self.status_label.setText("Status: Stopped")
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self._log("Stopped")

    def _poll(self):
        list_id = self.list_cb.currentData()
        if not list_id:
            return
        stats = self.db.mailing_stats(list_id)
        total = stats["total"]
        sent = stats["SENT"]
        failed = stats["FAILED"]
        excluded = stats.get("EXCLUDED", 0)
        processed = sent + failed + excluded

        self._metric_labels["Total"].setText(f"{total:,}")
        self._metric_labels["Sent"].setText(f"{sent:,}")
        self._metric_labels["Failed"].setText(f"{failed:,}")
        self._metric_labels["Excluded"].setText(f"{excluded:,}")

        pct = int(processed / total * 100) if total > 0 else 0
        self.progress.setValue(pct)

        elapsed = time.time() - self._started_at if self._running else 0
        speed = processed / elapsed if elapsed > 1 else 0
        self._metric_labels["Speed"].setText(f"{speed:.1f}/s")

        m, s = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        self._metric_labels["Elapsed"].setText(f"{h}:{m:02d}:{s:02d}")

        remaining = (total - processed) / speed if speed > 0 else 0
        m, s = divmod(int(remaining), 60)
        h, m = divmod(m, 60)
        self._metric_labels["ETA"].setText(f"{h}:{m:02d}:{s:02d}" if speed > 0 else "--:--")

        if not self._running and not self.btn_start.isEnabled():
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)
