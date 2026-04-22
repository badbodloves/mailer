"""Mailing Lists management tab with search, filter, bulk actions."""
import os
import re
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QListWidget, QListWidgetItem,
                                 QTableWidget, QTableWidgetItem, QPushButton,
                                 QLineEdit, QLabel, QFileDialog, QMessageBox,
                                 QHeaderView, QInputDialog, QCheckBox, QTextEdit)
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
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._search)
        search_row.addWidget(search_btn)
        rl.addLayout(search_row)

        # Exclude bar
        exclude_row = QHBoxLayout()
        exclude_row.addWidget(QLabel("Exclude domain:"))
        self.exclude_input = QLineEdit()
        self.exclude_input.setPlaceholderText("e.g. yahoo.de")
        exclude_row.addWidget(self.exclude_input)
        del_domain_btn = QPushButton("Delete all @domain")
        del_domain_btn.clicked.connect(self._delete_domain)
        exclude_row.addWidget(del_domain_btn)
        rl.addLayout(exclude_row)

        # Exclude Rules
        rules_box = QGroupBox("Global Exclude Rules (applied to all lists unless disabled)")
        rules_l = QVBoxLayout(rules_box)
        self.exclude_rules = QTextEdit()
        self.exclude_rules.setMaximumHeight(80)
        self.exclude_rules.setPlaceholderText(
            "One rule per line. Prefix with @ for domain, no prefix for local-part.\n"
            "Examples: @spam.com  spam@  datenschutz@  dsgvo@  noreply@  abuse@  postmaster@")
        self.exclude_rules.setPlainText(
            "@spam.com\n@junk.com\nspam@\ndatenschutz@\ndsgvo@\nnoreply@\nabuse@\npostmaster@\nmailer-daemon@")
        rules_l.addWidget(self.exclude_rules)
        rl.addWidget(rules_box)

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
        del_sel_btn = QPushButton("Delete Selected")
        del_sel_btn.clicked.connect(self._delete_selected)
        stats.addWidget(del_sel_btn)
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

    def _get_exclude_rules(self) -> list:
        rules = []
        for line in self.exclude_rules.toPlainText().splitlines():
            line = line.strip().lower()
            if line:
                rules.append(line)
        return rules

    def _should_exclude(self, email: str, rules: list) -> bool:
        email = email.lower()
        local = email.split("@")[0] if "@" in email else ""
        domain = email.split("@")[1] if "@" in email else ""
        for rule in rules:
            if rule.startswith("@") and domain == rule[1:]:
                return True
            if rule.endswith("@") and local == rule[:-1]:
                return True
            if rule in email:
                return True
        return False

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import Leads", "",
                                                  "Text Files (*.txt *.csv);;All (*)")
        if not paths:
            return

        rules = self._get_exclude_rules()
        summary = []

        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0]
            list_id = self.db.create_list(name, path)
            all_emails = []
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    for match in EMAIL_RE.findall(line):
                        all_emails.append(match.lower())
            filtered = [e for e in all_emails if not self._should_exclude(e, rules)]
            excluded_count = len(all_emails) - len(filtered)
            added = self.db.import_leads(list_id, filtered)
            summary.append(f"{name}: {added:,} leads" + (f" ({excluded_count} excluded)" if excluded_count else ""))

        self._refresh()
        QMessageBox.information(self, "Import Complete",
                                 f"{len(summary)} list(s) imported:\n\n" + "\n".join(summary))

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
