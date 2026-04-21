"""SMTP Presets management tab."""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QTableWidget, QTableWidgetItem, QPushButton,
                                 QFormLayout, QLineEdit, QSpinBox, QMessageBox,
                                 QHeaderView, QDialog, QDialogButtonBox)
from PySide6.QtCore import Qt


class SMTPTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Name", "Host", "Port", "Username", "Daily Limit", "Sent Today"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        btns = QHBoxLayout()
        for text, fn in [("Add SMTP", self._add), ("Edit", self._edit),
                         ("Delete", self._delete), ("Reset Daily", self._reset),
                         ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        layout.addLayout(btns)

        self._refresh()

    def _refresh(self):
        self.db.reset_daily_counts()
        rows = self.db.get_smtps()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r["name"]))
            self.table.setItem(i, 1, QTableWidgetItem(r["host"]))
            self.table.setItem(i, 2, QTableWidgetItem(str(r["port"])))
            self.table.setItem(i, 3, QTableWidgetItem(r["username"]))
            self.table.setItem(i, 4, QTableWidgetItem(str(r["daily_limit"])))
            self.table.setItem(i, 5, QTableWidgetItem(str(r["sent_today"])))
            for j in range(6):
                item = self.table.item(i, j)
                if item:
                    item.setData(Qt.UserRole, r["id"])

    def _get_selected_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _add(self):
        dlg = SMTPDialog(self)
        if dlg.exec() == QDialog.Accepted:
            d = dlg.get_data()
            self.db.add_smtp(**d)
            self._refresh()

    def _edit(self):
        sid = self._get_selected_id()
        if not sid:
            return
        row = self.db._conn().execute("SELECT * FROM smtp_presets WHERE id=?", (sid,)).fetchone()
        if not row:
            return
        dlg = SMTPDialog(self, row)
        if dlg.exec() == QDialog.Accepted:
            d = dlg.get_data()
            c = self.db._conn()
            c.execute("UPDATE smtp_presets SET name=?,host=?,port=?,username=?,password=?,daily_limit=? WHERE id=?",
                      (d["name"], d["host"], d["port"], d["username"], d["password"], d["daily_limit"], sid))
            c.commit()
            self._refresh()

    def _delete(self):
        sid = self._get_selected_id()
        if not sid:
            return
        if QMessageBox.question(self, "Delete", "Delete this SMTP preset?") == QMessageBox.Yes:
            self.db.delete_smtp(sid)
            self._refresh()

    def _reset(self):
        self.db.reset_daily_counts()
        self._refresh()


class SMTPDialog(QDialog):
    def __init__(self, parent, existing=None):
        super().__init__(parent)
        self.setWindowTitle("SMTP Preset")
        self.setMinimumWidth(400)
        layout = QFormLayout(self)

        self.name = QLineEdit(existing["name"] if existing else "")
        self.host = QLineEdit(existing["host"] if existing else "")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(existing["port"] if existing else 587)
        self.username = QLineEdit(existing["username"] if existing else "")
        self.password = QLineEdit(existing["password"] if existing else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.limit = QSpinBox()
        self.limit.setRange(0, 999999)
        self.limit.setValue(existing["daily_limit"] if existing else 50000)

        layout.addRow("Name:", self.name)
        layout.addRow("Host:", self.host)
        layout.addRow("Port:", self.port)
        layout.addRow("Username:", self.username)
        layout.addRow("Password:", self.password)
        layout.addRow("Daily Limit (0=unlimited):", self.limit)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def get_data(self) -> dict:
        return {
            "name": self.name.text(),
            "host": self.host.text(),
            "port": self.port.value(),
            "username": self.username.text(),
            "password": self.password.text(),
            "daily_limit": self.limit.value(),
        }
