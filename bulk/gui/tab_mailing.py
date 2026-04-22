"""Mailing control — multi-mailing with table overview."""
import json
import time
import threading
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QComboBox, QPushButton, QLabel, QSpinBox,
                                 QLineEdit, QProgressBar, QTextEdit,
                                 QMessageBox, QFormLayout, QSplitter,
                                 QTableWidget, QTableWidgetItem, QHeaderView,
                                 QInputDialog, QTimeEdit)
from PySide6.QtCore import Qt, QTimer, QTime


class MailingTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._threads = {}
        self._cores = {}
        layout = QVBoxLayout(self)

        # Top: mailing list table
        top = QGroupBox("Mailings")
        tl = QVBoxLayout(top)
        btn_row = QHBoxLayout()
        for text, fn in [("Add...", self._add_mailing), ("Start", self._start_selected),
                         ("Stop", self._stop_selected), ("Delete", self._delete_selected),
                         ("Refresh", self._refresh_table)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btn_row.addWidget(b)
        tl.addLayout(btn_row)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["Name", "Status", "Progress", "Total", "Sent", "Failed", "ETA"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemClicked.connect(self._on_select)
        tl.addWidget(self.table)
        layout.addWidget(top)

        # Bottom: selected mailing details
        bottom = QSplitter(Qt.Horizontal)

        detail = QGroupBox("Mailing Details")
        dl = QVBoxLayout(detail)
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(200)
        dl.addWidget(self.detail_text)
        bottom.addWidget(detail)

        events = QGroupBox("Events Log")
        el = QVBoxLayout(events)
        self.events_text = QTextEdit()
        self.events_text.setReadOnly(True)
        self.events_text.setStyleSheet("background:#1e1e1e; color:#d4d4d4; font-family:Consolas")
        el.addWidget(self.events_text)
        bottom.addWidget(events)

        layout.addWidget(bottom)

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._refresh_table)
        self._poll_timer.start(3000)

        self._refresh_table()

    def _log(self, msg):
        self.events_text.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _refresh_table(self):
        mailings = self.db.get_mailings()
        self.table.setRowCount(len(mailings))
        for i, m in enumerate(mailings):
            total = m["total_leads"] or 0
            sent = m["sent"] or 0
            failed = m["failed"] or 0
            pct = f"{(sent+failed)/total*100:.0f}%" if total > 0 else "0%"

            self.table.setItem(i, 0, QTableWidgetItem(f"Mailing #{m['id']}"))
            status_item = QTableWidgetItem(m["status"])
            self.table.setItem(i, 1, status_item)
            self.table.setItem(i, 2, QTableWidgetItem(pct))
            self.table.setItem(i, 3, QTableWidgetItem(f"{total:,}"))
            self.table.setItem(i, 4, QTableWidgetItem(f"{sent:,}"))
            self.table.setItem(i, 5, QTableWidgetItem(f"{failed:,}"))
            self.table.setItem(i, 6, QTableWidgetItem(""))
            for j in range(7):
                item = self.table.item(i, j)
                if item:
                    item.setData(Qt.UserRole, m["id"])

    def _get_selected_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_select(self):
        mid = self._get_selected_id()
        if not mid:
            return
        m = self.db._conn().execute("SELECT * FROM mailings WHERE id=?", (mid,)).fetchone()
        if not m:
            return
        info = (f"Mailing #{m['id']}  Status: {m['status']}\n"
                f"Brand: {m['brand_id']}  Domain: {m['domain_id']}  SMTP: {m['smtp_preset_id']}\n"
                f"List: {m['list_id']}  Template: {m['template_id']}\n"
                f"Daily Limit: {m['daily_limit']}\n"
                f"Excludes: {m['exclude_domains_json']}\n"
                f"Created: {m['created_at']}  Started: {m['started_at'] or '-'}\n"
                f"Sent: {m['sent']}  Failed: {m['failed']}  Excluded: {m['excluded']}")
        self.detail_text.setPlainText(info)

    def _add_mailing(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("New Mailing")
        dlg.setMinimumWidth(500)
        fl = QFormLayout(dlg)

        brand_cb = QComboBox()
        domain_cb = QComboBox()
        list_cb = QComboBox()
        smtp_cb = QComboBox()
        tmpl_cb = QComboBox()
        sender_input = QLineEdit()
        sender_input.setPlaceholderText("Display name or {macro}")

        daily_cb = QComboBox()
        daily_cb.addItems(["Use SMTP limit", "10,000", "25,000", "50,000", "100,000", "Custom..."])
        daily_spin = QSpinBox()
        daily_spin.setRange(0, 999999)
        daily_spin.setVisible(False)
        daily_cb.currentTextChanged.connect(lambda t: daily_spin.setVisible(t == "Custom..."))

        exclude_input = QLineEdit()
        exclude_input.setPlaceholderText("yahoo.de, aol.com")
        test_input = QLineEdit()
        test_input.setPlaceholderText("test@inbox.com")
        test_interval = QSpinBox()
        test_interval.setRange(0, 999999)
        test_interval.setValue(1000)
        test_interval.setSpecialValueText("Disabled")
        schedule_time = QTimeEdit()
        schedule_time.setDisplayFormat("HH:mm")

        for b in self.db.get_brands():
            brand_cb.addItem(b["name"], b["id"])
        def on_brand():
            domain_cb.clear()
            bid = brand_cb.currentData()
            if bid:
                for d in self.db.get_domains(bid):
                    domain_cb.addItem(d["domain"], d["id"])
        brand_cb.currentIndexChanged.connect(on_brand)
        on_brand()

        for l in self.db.get_lists():
            cnt = self.db.get_list_lead_count(l["id"])
            list_cb.addItem(f"{l['name']} ({cnt:,})", l["id"])
        self.db.reset_daily_counts()
        for s in self.db.get_smtps():
            rem = self.db.get_smtp_remaining(s["id"])
            smtp_cb.addItem(f"{s['name']} ({rem:,} left)", s["id"])
        for t in self.db.get_templates():
            tmpl_cb.addItem(t["name"], t["id"])

        fl.addRow("Brand:", brand_cb)
        fl.addRow("Domain:", domain_cb)
        fl.addRow("Sender Name:", sender_input)
        fl.addRow("Mailing List:", list_cb)
        fl.addRow("SMTP:", smtp_cb)
        fl.addRow("Template:", tmpl_cb)
        fl.addRow("Daily Limit:", daily_cb)
        fl.addRow("Custom Limit:", daily_spin)
        fl.addRow("Exclude Domains:", exclude_input)
        fl.addRow("Test Mail To:", test_input)
        fl.addRow("Test Every N:", test_interval)
        fl.addRow("Schedule:", schedule_time)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        fl.addRow(btns)

        if dlg.exec() != QDialog.Accepted:
            return

        limit_map = {"Use SMTP limit": 0, "10,000": 10000, "25,000": 25000,
                     "50,000": 50000, "100,000": 100000, "Custom...": daily_spin.value()}
        daily = limit_map.get(daily_cb.currentText(), 0)
        excludes = [d.strip() for d in exclude_input.text().split(",") if d.strip()]

        mid = self.db.create_mailing(
            brand_id=brand_cb.currentData(),
            domain_id=domain_cb.currentData(),
            list_id=list_cb.currentData(),
            smtp_preset_id=smtp_cb.currentData(),
            template_id=tmpl_cb.currentData(),
            daily_limit=daily,
            exclude_domains_json=json.dumps(excludes),
            total_leads=self.db.get_list_lead_count(list_cb.currentData() or 0),
        )
        self._log(f"Mailing #{mid} created")
        self._refresh_table()

    def _start_selected(self):
        mid = self._get_selected_id()
        if not mid:
            return
        if mid in self._cores:
            QMessageBox.warning(self, "Running", "This mailing is already running")
            return

        from bulk.mailer.bulk_core import BulkMailerCore
        core = BulkMailerCore(self.db, mid)
        self._cores[mid] = core

        def run():
            try:
                core.run()
                self._log(f"Mailing #{mid} finished")
            except Exception as e:
                self._log(f"Mailing #{mid} ERROR: {e}")
            finally:
                self._cores.pop(mid, None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._threads[mid] = t
        self._log(f"Mailing #{mid} started")
        self._refresh_table()

    def _stop_selected(self):
        mid = self._get_selected_id()
        if not mid:
            return
        core = self._cores.get(mid)
        if core:
            core.stop()
            self._log(f"Mailing #{mid} stopped")
        self._refresh_table()

    def _delete_selected(self):
        mid = self._get_selected_id()
        if not mid:
            return
        if mid in self._cores:
            QMessageBox.warning(self, "Running", "Stop the mailing first")
            return
        if QMessageBox.question(self, "Delete", f"Delete mailing #{mid}?") == QMessageBox.Yes:
            c = self.db._conn()
            c.execute("DELETE FROM mailings WHERE id=?", (mid,))
            c.commit()
            self._refresh_table()
