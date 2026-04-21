"""Brands & Domains management tab."""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QTreeWidget, QTreeWidgetItem,
                                 QPushButton, QLineEdit, QLabel, QFormLayout,
                                 QMessageBox, QInputDialog, QHeaderView)
from PySide6.QtCore import Qt


class BrandsTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        layout = QHBoxLayout(self)

        # Left: brand tree
        left = QGroupBox("Brands & Domains")
        ll = QVBoxLayout(left)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Details"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.itemClicked.connect(self._on_select)
        ll.addWidget(self.tree)

        btns = QHBoxLayout()
        for text, fn in [("Add Brand", self._add_brand), ("Add Domain", self._add_domain),
                         ("Delete", self._delete), ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        ll.addLayout(btns)

        # Right: details + list usage
        right = QGroupBox("Details")
        rl = QVBoxLayout(right)
        self.detail_form = QFormLayout()
        self.lbl_name = QLabel("-")
        self.lbl_type = QLabel("-")
        self.detail_form.addRow("Name:", self.lbl_name)
        self.detail_form.addRow("Type:", self.lbl_type)

        self.edit_from = QLineEdit()
        self.edit_reply = QLineEdit()
        self.edit_bounce = QLineEdit()
        self.edit_listid = QLineEdit()
        self.detail_form.addRow("From Email:", self.edit_from)
        self.detail_form.addRow("Reply-To:", self.edit_reply)
        self.detail_form.addRow("Bounce Sub:", self.edit_bounce)
        self.detail_form.addRow("List-ID:", self.edit_listid)
        rl.addLayout(self.detail_form)

        save_btn = QPushButton("Save Domain Settings")
        save_btn.clicked.connect(self._save_domain)
        rl.addWidget(save_btn)

        # List usage
        usage_box = QGroupBox("List Usage")
        ul = QVBoxLayout(usage_box)
        self.used_label = QLabel("Used lists: -")
        self.unused_label = QLabel("Unused lists: -")
        ul.addWidget(self.used_label)
        ul.addWidget(self.unused_label)
        rl.addWidget(usage_box)
        rl.addStretch()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([500, 400])
        layout.addWidget(splitter)

        self._refresh()

    def _refresh(self):
        self.tree.clear()
        for brand in self.db.get_brands():
            item = QTreeWidgetItem([brand["name"], "Brand", f"ID: {brand['id']}"])
            item.setData(0, Qt.UserRole, ("brand", brand["id"]))
            for dom in self.db.get_domains(brand["id"]):
                child = QTreeWidgetItem([dom["domain"], "Domain", dom["from_email"] or "-"])
                child.setData(0, Qt.UserRole, ("domain", dom["id"]))
                item.addChild(child)
            item.setExpanded(True)
            self.tree.addTopLevelItem(item)

    def _add_brand(self):
        name, ok = QInputDialog.getText(self, "Add Brand", "Brand name:")
        if ok and name.strip():
            try:
                self.db.add_brand(name.strip())
                self._refresh()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _add_domain(self):
        item = self.tree.currentItem()
        if not item:
            QMessageBox.warning(self, "Select", "Select a brand first")
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == "domain":
            parent = item.parent()
            if parent:
                data = parent.data(0, Qt.UserRole)
        if data[0] != "brand":
            return
        domain, ok = QInputDialog.getText(self, "Add Domain", "Domain (e.g. news.example.com):")
        if ok and domain.strip():
            try:
                self.db.add_domain(data[1], domain.strip())
                self._refresh()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _delete(self):
        item = self.tree.currentItem()
        if not item:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if QMessageBox.question(self, "Delete", f"Delete {data[0]} '{item.text(0)}'?") != QMessageBox.Yes:
            return
        if data[0] == "brand":
            self.db.delete_brand(data[1])
        elif data[0] == "domain":
            self.db.delete_domain(data[1])
        self._refresh()

    def _on_select(self, item):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        self.lbl_name.setText(item.text(0))
        self.lbl_type.setText(data[0])

        if data[0] == "brand":
            used = self.db.get_used_lists(data[1])
            unused = self.db.get_unused_lists(data[1])
            self.used_label.setText(f"Used lists: {len(used)} — " + ", ".join(r['name'] for r in used[:10]))
            self.unused_label.setText(f"Unused lists: {len(unused)} — " + ", ".join(r['name'] for r in unused[:10]))
            self.edit_from.clear()
            self.edit_reply.clear()

        elif data[0] == "domain":
            row = self.db._conn().execute("SELECT * FROM domains WHERE id=?", (data[1],)).fetchone()
            if row:
                self.edit_from.setText(row["from_email"] or "")
                self.edit_reply.setText(row["reply_to"] or "")
                self.edit_bounce.setText(row["bounce_subdomain"] or "bounce")
                self.edit_listid.setText(row["list_id_label"] or "")

    def _save_domain(self):
        item = self.tree.currentItem()
        if not item:
            return
        data = item.data(0, Qt.UserRole)
        if not data or data[0] != "domain":
            QMessageBox.warning(self, "Select", "Select a domain to save")
            return
        c = self.db._conn()
        c.execute("UPDATE domains SET from_email=?, reply_to=?, bounce_subdomain=?, list_id_label=? WHERE id=?",
                  (self.edit_from.text(), self.edit_reply.text(),
                   self.edit_bounce.text(), self.edit_listid.text(), data[1]))
        c.commit()
        self._refresh()
        QMessageBox.information(self, "Saved", "Domain settings saved")
