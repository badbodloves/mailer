"""Mailing Lists management tab with search, filter, bulk actions."""
import os
import re
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QListWidget, QListWidgetItem,
                                 QTableWidget, QTableWidgetItem, QPushButton,
                                 QLineEdit, QLabel, QFileDialog, QMessageBox,
                                 QHeaderView, QInputDialog, QCheckBox)
from PySide6.QtCore import Qt

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


class ListsTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._current_list_id = None
        layout = QHBoxLayout(self)

        # Left: list of lists
        left = QGroupBox("Mailing Lists")
        ll = QVBoxLayout(left)
        self.list_widget = QListWidget()
        self.list_widget.itemClicked.connect(self._on_list_select)
        ll.addWidget(self.list_widget)

        bl = QHBoxLayout()
        for text, fn in [("Import", self._import), ("Delete List", self._delete_list),
                         ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bl.addWidget(b)
        ll.addLayout(bl)

        # Right: lead viewer
        right = QWidget()
        rl = QVBoxLayout(right)

        # Search bar
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search emails...")
        self.search_input.returnPressed.connect(self._search)
        search_row.addWidget(self.search_input)
        QPushButton("Search").clicked.connect(self._search)
        search_row.addWidget(QPushButton("Search", clicked=self._search))
        rl.addLayout(search_row)

        # Exclude bar
        exclude_row = QHBoxLayout()
        exclude_row.addWidget(QLabel("Exclude domain:"))
        self.exclude_input = QLineEdit()
        self.exclude_input.setPlaceholderText("e.g. yahoo.de")
        exclude_row.addWidget(self.exclude_input)
        QPushButton("Delete all @domain").clicked.connect(self._delete_domain)
        exclude_row.addWidget(QPushButton("Delete all @domain", clicked=self._delete_domain))
        rl.addLayout(exclude_row)

        # Table
        self.lead_table = QTableWidget()
        self.lead_table.setColumnCount(3)
        self.lead_table.setHorizontalHeaderLabels(["ID", "Email", "State"])
        self.lead_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.lead_table.setSelectionBehavior(QTableWidget.SelectRows)
        rl.addWidget(self.lead_table)

        # Stats + actions
        stats = QHBoxLayout()
        self.stats_label = QLabel("Select a list")
        stats.addWidget(self.stats_label)
        stats.addStretch()
        QPushButton("Delete Selected").clicked.connect(self._delete_selected)
        stats.addWidget(QPushButton("Delete Selected", clicked=self._delete_selected))
        rl.addLayout(stats)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([300, 700])
        layout.addWidget(splitter)

        self._refresh()

    def _refresh(self):
        self.list_widget.clear()
        for lst in self.db.get_lists():
            count = self.db.get_list_lead_count(lst["id"])
            item = QListWidgetItem(f"{lst['name']}  ({count:,} leads)")
            item.setData(Qt.UserRole, lst["id"])
            self.list_widget.addItem(item)

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import Leads", "",
                                                  "Text Files (*.txt *.csv);;All (*)")
        if not paths:
            return
        exclude_str, ok = QInputDialog.getText(self, "Exclude Domains",
                                                "Exclude domains (comma-sep, e.g. yahoo.de,aol.com):")
        excludes = [d.strip().lower() for d in exclude_str.split(",") if d.strip()] if ok else []

        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0]
            list_id = self.db.create_list(name, path)
            emails = []
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    for match in EMAIL_RE.findall(line):
                        emails.append(match.lower())
            added = self.db.import_leads(list_id, emails, excludes)
            QMessageBox.information(self, "Imported",
                                     f"{name}: {added:,} leads imported"
                                     + (f" ({len(emails)-added} excluded)" if excludes else ""))
        self._refresh()

    def _delete_list(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        lid = item.data(Qt.UserRole)
        if QMessageBox.question(self, "Delete", f"Delete list '{item.text()}'?") == QMessageBox.Yes:
            self.db.delete_list(lid)
            self._current_list_id = None
            self._refresh()

    def _on_list_select(self, item):
        lid = item.data(Qt.UserRole)
        self._current_list_id = lid
        self._load_leads(lid)

    def _load_leads(self, list_id, query=""):
        if query:
            rows = self.db.search_leads(list_id, query)
        else:
            rows = self.db._conn().execute(
                "SELECT id, email, state FROM leads WHERE list_id=? LIMIT 1000",
                (list_id,)).fetchall()

        self.lead_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.lead_table.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.lead_table.setItem(i, 1, QTableWidgetItem(r["email"]))
            self.lead_table.setItem(i, 2, QTableWidgetItem(r["state"]))

        stats = self.db.mailing_stats(list_id)
        self.stats_label.setText(
            f"Total: {stats['total']:,} | Pending: {stats['PENDING']:,} | "
            f"Sent: {stats['SENT']:,} | Failed: {stats['FAILED']:,} | "
            f"Excluded: {stats.get('EXCLUDED', 0):,}")

    def _search(self):
        if not self._current_list_id:
            return
        self._load_leads(self._current_list_id, self.search_input.text())

    def _delete_domain(self):
        if not self._current_list_id:
            return
        domain = self.exclude_input.text().strip()
        if not domain:
            return
        deleted = self.db.delete_leads_by_domain(self._current_list_id, domain)
        QMessageBox.information(self, "Deleted", f"Removed {deleted:,} leads with @{domain}")
        self._load_leads(self._current_list_id)
        self._refresh()

    def _delete_selected(self):
        rows = set(idx.row() for idx in self.lead_table.selectedIndexes())
        if not rows:
            return
        ids = []
        for r in rows:
            item = self.lead_table.item(r, 0)
            if item:
                ids.append(int(item.text()))
        if ids and QMessageBox.question(self, "Delete", f"Delete {len(ids)} leads?") == QMessageBox.Yes:
            self.db.delete_leads_by_ids(ids)
            if self._current_list_id:
                self._load_leads(self._current_list_id)
            self._refresh()
