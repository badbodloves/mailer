"""Cloudflare Tab — R2 asset uploads + Worker deployment + Zone management."""
import os
import json
import threading
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QTabWidget, QFormLayout, QLineEdit,
                                 QPushButton, QLabel, QComboBox, QListWidget,
                                 QListWidgetItem, QTextEdit, QFileDialog,
                                 QMessageBox, QProgressBar, QHeaderView,
                                 QTableWidget, QTableWidgetItem)
from PySide6.QtCore import Qt


class CloudflareTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._r2 = None
        layout = QVBoxLayout(self)

        # Credentials
        creds = QGroupBox("Cloudflare Credentials")
        cf = QFormLayout(creds)
        self.account_id = QLineEdit()
        self.account_id.setPlaceholderText("Cloudflare Account ID")
        cf.addRow("Account ID:", self.account_id)
        self.api_token = QLineEdit()
        self.api_token.setEchoMode(QLineEdit.Password)
        self.api_token.setPlaceholderText("API Token (R2 + Workers + Zones)")
        cf.addRow("API Token:", self.api_token)
        self.r2_access_key = QLineEdit()
        self.r2_access_key.setPlaceholderText("R2 Access Key ID")
        cf.addRow("R2 Access Key:", self.r2_access_key)
        self.r2_secret_key = QLineEdit()
        self.r2_secret_key.setEchoMode(QLineEdit.Password)
        self.r2_secret_key.setPlaceholderText("R2 Secret Access Key")
        cf.addRow("R2 Secret Key:", self.r2_secret_key)

        cred_btns = QHBoxLayout()
        cred_btns.addWidget(QPushButton("Connect", clicked=self._connect))
        cred_btns.addWidget(QPushButton("Save Credentials", clicked=self._save_creds))
        cred_btns.addWidget(QPushButton("Load Credentials", clicked=self._load_creds))
        cf.addRow(cred_btns)

        self.status_label = QLabel("Not connected")
        cf.addRow("Status:", self.status_label)
        layout.addWidget(creds)

        # Tabs for R2 and Workers
        tabs = QTabWidget()

        # R2 Tab
        r2_tab = QWidget()
        r2l = QVBoxLayout(r2_tab)

        r2_top = QHBoxLayout()
        r2_top.addWidget(QLabel("Bucket:"))
        self.bucket_cb = QComboBox()
        self.bucket_cb.setMinimumWidth(200)
        r2_top.addWidget(self.bucket_cb)
        r2_top.addWidget(QPushButton("Refresh", clicked=self._refresh_buckets))
        r2_top.addWidget(QPushButton("Create Bucket", clicked=self._create_bucket))
        r2_top.addWidget(QPushButton("Enable Public", clicked=self._enable_public))
        r2_top.addStretch()
        r2l.addLayout(r2_top)

        domain_row = QHBoxLayout()
        domain_row.addWidget(QLabel("Custom Domain:"))
        self.custom_domain = QLineEdit()
        self.custom_domain.setPlaceholderText("cdn.yourdomain.com")
        domain_row.addWidget(self.custom_domain)
        domain_row.addWidget(QPushButton("Add Domain", clicked=self._add_custom_domain))
        r2l.addLayout(domain_row)

        # File list + upload
        r2_mid = QHBoxLayout()

        file_box = QGroupBox("Files in Bucket")
        fl = QVBoxLayout(file_box)
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(2)
        self.file_table.setHorizontalHeaderLabels(["Key", "Size"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        fl.addWidget(self.file_table)

        file_btns = QHBoxLayout()
        file_btns.addWidget(QPushButton("Refresh Files", clicked=self._refresh_files))
        file_btns.addWidget(QPushButton("Delete Selected", clicked=self._delete_file))
        fl.addLayout(file_btns)
        r2_mid.addWidget(file_box)

        upload_box = QGroupBox("Upload")
        ul = QVBoxLayout(upload_box)
        ul.addWidget(QLabel("Upload files to selected bucket:"))
        self.upload_prefix = QLineEdit()
        self.upload_prefix.setPlaceholderText("Subfolder prefix (e.g. logos/)")
        ul.addWidget(self.upload_prefix)
        ul.addWidget(QPushButton("Select & Upload Files", clicked=self._upload_files))
        self.upload_progress = QProgressBar()
        ul.addWidget(self.upload_progress)
        self.upload_log = QTextEdit()
        self.upload_log.setReadOnly(True)
        self.upload_log.setMaximumHeight(120)
        self.upload_log.setStyleSheet("font-family:Consolas; font-size:10px")
        ul.addWidget(self.upload_log)
        r2_mid.addWidget(upload_box)

        r2l.addLayout(r2_mid)
        tabs.addTab(r2_tab, "R2 Storage")

        # Worker Tab
        worker_tab = QWidget()
        wl = QVBoxLayout(worker_tab)

        wl.addWidget(QLabel("Deploy Unsubscribe Worker to Cloudflare:"))

        zone_row = QHBoxLayout()
        zone_row.addWidget(QLabel("Zone:"))
        self.zone_cb = QComboBox()
        self.zone_cb.setMinimumWidth(250)
        zone_row.addWidget(self.zone_cb)
        zone_row.addWidget(QPushButton("Refresh Zones", clicked=self._refresh_zones))
        zone_row.addStretch()
        wl.addLayout(zone_row)

        worker_row = QHBoxLayout()
        worker_row.addWidget(QLabel("Worker Name:"))
        self.worker_name = QLineEdit("unsub-worker")
        worker_row.addWidget(self.worker_name)
        worker_row.addWidget(QLabel("KV Namespace:"))
        self.kv_name = QLineEdit("unsubscribes")
        worker_row.addWidget(self.kv_name)
        wl.addLayout(worker_row)

        route_row = QHBoxLayout()
        route_row.addWidget(QLabel("Route Pattern:"))
        self.route_pattern = QLineEdit()
        self.route_pattern.setPlaceholderText("unsub.yourdomain.com/*")
        route_row.addWidget(self.route_pattern)
        wl.addLayout(route_row)

        deploy_btns = QHBoxLayout()
        deploy_btns.addWidget(QPushButton("Deploy Worker", clicked=self._deploy_worker))
        deploy_btns.addWidget(QPushButton("Check Status", clicked=self._check_worker))
        deploy_btns.addStretch()
        wl.addLayout(deploy_btns)

        self.worker_log = QTextEdit()
        self.worker_log.setReadOnly(True)
        self.worker_log.setStyleSheet("font-family:Consolas; background:#1e1e1e; color:#d4d4d4")
        wl.addWidget(self.worker_log)

        tabs.addTab(worker_tab, "Unsubscribe Worker")
        layout.addWidget(tabs)

        self._load_creds()

    def _wlog(self, msg):
        self.worker_log.append(msg)

    def _ulog(self, msg):
        self.upload_log.append(msg)

    def _get_r2(self):
        if self._r2 and self._r2.enabled:
            return self._r2
        from bulk.mailer.r2_manager import R2Manager
        self._r2 = R2Manager(
            account_id=self.account_id.text().strip(),
            api_token=self.api_token.text().strip(),
            access_key_id=self.r2_access_key.text().strip(),
            secret_access_key=self.r2_secret_key.text().strip(),
        )
        return self._r2

    def _connect(self):
        r2 = self._get_r2()
        if r2.enabled:
            buckets = r2.list_buckets()
            self.status_label.setText(f"Connected — {len(buckets)} buckets")
            self.status_label.setStyleSheet("color:green; font-weight:bold")
            self._refresh_buckets()
            self._refresh_zones()
        else:
            self.status_label.setText("Failed — check credentials")
            self.status_label.setStyleSheet("color:red")

    def _save_creds(self):
        data = {
            "account_id": self.account_id.text(),
            "api_token": self.api_token.text(),
            "r2_access_key": self.r2_access_key.text(),
            "r2_secret_key": self.r2_secret_key.text(),
        }
        with open("cf_credentials.json", "w") as f:
            json.dump(data, f, indent=2)
        QMessageBox.information(self, "Saved", "Credentials saved to cf_credentials.json")

    def _load_creds(self):
        if not os.path.isfile("cf_credentials.json"):
            return
        try:
            with open("cf_credentials.json", "r") as f:
                data = json.load(f)
            self.account_id.setText(data.get("account_id", ""))
            self.api_token.setText(data.get("api_token", ""))
            self.r2_access_key.setText(data.get("r2_access_key", ""))
            self.r2_secret_key.setText(data.get("r2_secret_key", ""))
        except Exception:
            pass

    def _refresh_buckets(self):
        r2 = self._get_r2()
        self.bucket_cb.clear()
        for name in r2.list_buckets():
            self.bucket_cb.addItem(name)

    def _create_bucket(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Create Bucket", "Bucket name:")
        if ok and name.strip():
            r2 = self._get_r2()
            if r2.create_bucket(name.strip()):
                self._refresh_buckets()
                QMessageBox.information(self, "Created", f"Bucket '{name}' created")
            else:
                QMessageBox.warning(self, "Error", "Failed to create bucket")

    def _enable_public(self):
        bucket = self.bucket_cb.currentText()
        if not bucket:
            return
        r2 = self._get_r2()
        if r2.enable_public_access(bucket):
            QMessageBox.information(self, "Public", f"Public access enabled for '{bucket}'")
        else:
            QMessageBox.warning(self, "Error", "Failed — check API token permissions")

    def _add_custom_domain(self):
        bucket = self.bucket_cb.currentText()
        domain = self.custom_domain.text().strip()
        if not bucket or not domain:
            return
        r2 = self._get_r2()
        if r2.add_custom_domain(bucket, domain):
            QMessageBox.information(self, "Domain", f"Custom domain '{domain}' added to '{bucket}'")
        else:
            QMessageBox.warning(self, "Error", "Failed — ensure DNS is configured")

    def _refresh_files(self):
        bucket = self.bucket_cb.currentText()
        if not bucket:
            return
        r2 = self._get_r2()
        objects = r2.list_objects(bucket, self.upload_prefix.text())
        self.file_table.setRowCount(len(objects))
        for i, obj in enumerate(objects):
            self.file_table.setItem(i, 0, QTableWidgetItem(obj["key"]))
            size_kb = obj["size"] / 1024
            self.file_table.setItem(i, 1, QTableWidgetItem(f"{size_kb:.1f} KB"))

    def _delete_file(self):
        bucket = self.bucket_cb.currentText()
        row = self.file_table.currentRow()
        if not bucket or row < 0:
            return
        key = self.file_table.item(row, 0).text()
        if QMessageBox.question(self, "Delete", f"Delete '{key}'?") == QMessageBox.Yes:
            r2 = self._get_r2()
            r2.delete_object(bucket, key)
            self._refresh_files()

    def _upload_files(self):
        bucket = self.bucket_cb.currentText()
        if not bucket:
            QMessageBox.warning(self, "Bucket", "Select a bucket first")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Select Files", "",
                                                  "All Files (*)")
        if not paths:
            return

        prefix = self.upload_prefix.text().strip()
        self.upload_progress.setMaximum(len(paths))
        self.upload_progress.setValue(0)
        self.upload_log.clear()

        def upload():
            r2 = self._get_r2()
            for i, path in enumerate(paths):
                name = os.path.basename(path)
                key = f"{prefix}{name}" if prefix else name
                import mimetypes
                ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
                url = r2.upload_file(bucket, key, path, ct)
                if url:
                    self._ulog(f"✓ {key} → {url}")
                else:
                    self._ulog(f"✗ {key} — upload failed")
                self.upload_progress.setValue(i + 1)
            self._ulog(f"\nDone: {len(paths)} files")

        threading.Thread(target=upload, daemon=True).start()

    def _refresh_zones(self):
        r2 = self._get_r2()
        self.zone_cb.clear()
        for zone in r2.list_zones():
            self.zone_cb.addItem(f"{zone['name']} ({zone['id'][:8]}...)", zone["id"])

    def _deploy_worker(self):
        account_id = self.account_id.text().strip()
        api_token = self.api_token.text().strip()
        worker_name = self.worker_name.text().strip()
        kv_name = self.kv_name.text().strip()

        if not all([account_id, api_token, worker_name, kv_name]):
            QMessageBox.warning(self, "Missing", "Fill all fields")
            return

        self._wlog("Deploying worker...")

        def deploy():
            import requests
            headers = {"Authorization": f"Bearer {api_token}",
                       "Content-Type": "application/json"}

            # Step 1: Create KV namespace
            self._wlog("Creating KV namespace...")
            resp = requests.post(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
                headers=headers, json={"title": kv_name}, timeout=30)

            if resp.status_code == 200 and resp.json().get("success"):
                ns_id = resp.json()["result"]["id"]
                self._wlog(f"KV namespace created: {ns_id}")
            elif "already exists" in resp.text.lower():
                # Get existing
                resp2 = requests.get(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
                    headers=headers, timeout=30)
                ns_id = None
                for ns in resp2.json().get("result", []):
                    if ns["title"] == kv_name:
                        ns_id = ns["id"]
                        break
                if ns_id:
                    self._wlog(f"KV namespace exists: {ns_id}")
                else:
                    self._wlog("ERROR: Could not find existing namespace")
                    return
            else:
                self._wlog(f"ERROR: {resp.status_code} {resp.text[:200]}")
                return

            # Step 2: Upload worker script
            self._wlog("Uploading worker script...")
            worker_path = os.path.join(os.path.dirname(__file__), "..", "cloudflare", "unsubscribe-worker.js")
            if not os.path.isfile(worker_path):
                self._wlog(f"ERROR: Worker script not found at {worker_path}")
                return

            with open(worker_path, "r") as f:
                worker_code = f.read()

            metadata = json.dumps({
                "main_module": "worker.mjs",
                "compatibility_date": "2026-04-01",
                "bindings": [{
                    "type": "kv_namespace",
                    "name": "UNSUB_KV",
                    "namespace_id": ns_id,
                }],
            })

            resp = requests.put(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                headers={"Authorization": f"Bearer {api_token}"},
                files={
                    "metadata": ("metadata.json", metadata, "application/json"),
                    "worker.mjs": ("worker.mjs", worker_code, "application/javascript+module"),
                }, timeout=30)

            if resp.status_code == 200 and resp.json().get("success"):
                self._wlog("Worker deployed successfully!")
            else:
                self._wlog(f"ERROR: {resp.status_code} {resp.text[:300]}")
                return

            # Step 3: Add route if zone selected
            zone_id = self.zone_cb.currentData()
            route = self.route_pattern.text().strip()
            if zone_id and route:
                self._wlog(f"Adding route: {route}")
                resp = requests.post(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/workers/routes",
                    headers=headers,
                    json={"pattern": route, "script": worker_name},
                    timeout=30)
                if resp.status_code in (200, 201):
                    self._wlog(f"Route added: {route}")
                else:
                    self._wlog(f"Route error: {resp.status_code} {resp.text[:200]}")

            self._wlog("\nDeployment complete!")

        threading.Thread(target=deploy, daemon=True).start()

    def _check_worker(self):
        account_id = self.account_id.text().strip()
        api_token = self.api_token.text().strip()
        worker_name = self.worker_name.text().strip()
        if not all([account_id, api_token, worker_name]):
            return

        def check():
            import requests
            headers = {"Authorization": f"Bearer {api_token}"}
            resp = requests.get(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                headers=headers, timeout=15)
            if resp.status_code == 200:
                self._wlog(f"Worker '{worker_name}' is deployed and active")
            else:
                self._wlog(f"Worker '{worker_name}' not found ({resp.status_code})")

        threading.Thread(target=check, daemon=True).start()
