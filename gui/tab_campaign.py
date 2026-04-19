import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .helpers import db_stats, scan_files, read_config


class CampaignTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._core = None
        self._thread = None
        self._running = False
        self._started_at = 0.0
        self._build_ui()
        self._poll_stats()

    def _build_ui(self):
        top = ttk.LabelFrame(self, text="File Selection", padding=10)
        top.pack(fill="x", padx=10, pady=5)

        ttk.Label(top, text="Leads:").grid(row=0, column=0, sticky="w")
        self._leads_var = tk.StringVar()
        self._leads_cb = ttk.Combobox(top, textvariable=self._leads_var, state="readonly", width=40)
        self._leads_cb.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(top, text="SMTPs:").grid(row=1, column=0, sticky="w")
        self._smtp_var = tk.StringVar()
        self._smtp_cb = ttk.Combobox(top, textvariable=self._smtp_var, state="readonly", width=40)
        self._smtp_cb.grid(row=1, column=1, padx=5, pady=2)

        ttk.Button(top, text="Refresh", command=self._refresh_files).grid(row=0, column=2, rowspan=2, padx=10)
        self._refresh_files()

        ctrl = ttk.LabelFrame(self, text="Control", padding=10)
        ctrl.pack(fill="x", padx=10, pady=5)

        self._btn_start = ttk.Button(ctrl, text="▶ START", command=self._start)
        self._btn_start.pack(side="left", padx=5)
        self._btn_pause = ttk.Button(ctrl, text="⏸ PAUSE", command=self._pause, state="disabled")
        self._btn_pause.pack(side="left", padx=5)
        self._btn_stop = ttk.Button(ctrl, text="⏹ STOP", command=self._stop, state="disabled")
        self._btn_stop.pack(side="left", padx=5)
        self._btn_test = ttk.Button(ctrl, text="📧 Test Mail", command=self._test_mail)
        self._btn_test.pack(side="left", padx=5)

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(ctrl, textvariable=self._status_var, font=("", 10, "bold")).pack(side="right", padx=10)

        stats = ttk.LabelFrame(self, text="Live Stats", padding=10)
        stats.pack(fill="both", expand=True, padx=10, pady=5)

        self._progress = ttk.Progressbar(stats, length=500, mode="determinate")
        self._progress.pack(fill="x", pady=5)

        metrics = ttk.Frame(stats)
        metrics.pack(fill="x")
        self._lbl = {}
        for i, key in enumerate(["Total", "Sent", "Failed", "Pending", "Speed", "ETA"]):
            ttk.Label(metrics, text=f"{key}:").grid(row=0, column=i*2, sticky="e", padx=(10,2))
            lbl = ttk.Label(metrics, text="0", font=("", 10, "bold"))
            lbl.grid(row=0, column=i*2+1, sticky="w")
            self._lbl[key] = lbl

    def _refresh_files(self):
        leads = scan_files("Leads") + scan_files("leads") + scan_files(".", (".txt",))
        smtps = scan_files("SMTPs") + scan_files("smtps") + scan_files(".", (".txt",))
        cp = read_config()
        lf = cp.get("paths", "leads_file", fallback="leads.txt")
        sf = cp.get("paths", "smtp_file", fallback="smtps.txt")
        if lf not in leads:
            leads.insert(0, lf)
        if sf not in smtps:
            smtps.insert(0, sf)
        self._leads_cb["values"] = leads
        self._smtp_cb["values"] = smtps
        if leads:
            self._leads_var.set(leads[0])
        if smtps:
            self._smtp_var.set(smtps[0])

    def _start(self):
        if self._running:
            return
        leads = self._leads_var.get()
        smtp = self._smtp_var.get()
        if not leads or not smtp:
            messagebox.showerror("Error", "Select leads and SMTP files")
            return

        from mailer.mailer_core import MailerCore
        overrides = {"paths.leads_file": leads, "paths.smtp_file": smtp}

        def run():
            try:
                self._core = MailerCore(config_path="config.ini", overrides=overrides)
                self._core.run()
            except Exception as e:
                self._status_var.set(f"Error: {e}")
            finally:
                self._running = False
                self._core = None

        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._btn_start.config(state="disabled")
        self._btn_pause.config(state="normal")
        self._btn_stop.config(state="normal")
        self._status_var.set("Running")

    def _pause(self):
        if self._core:
            self._core.stop()
            self._status_var.set("Paused")
            self._btn_start.config(state="normal")
            self._btn_pause.config(state="disabled")

    def _stop(self):
        if self._core:
            self._core.force_stop()
        self._running = False
        self._status_var.set("Stopped")
        self._btn_start.config(state="normal")
        self._btn_pause.config(state="disabled")
        self._btn_stop.config(state="disabled")

    def _test_mail(self):
        cp = read_config()
        recipients = cp.get("test", "test_recipients", fallback="")
        if not recipients.strip():
            messagebox.showwarning("No recipients", "Set test_recipients in config.ini")
            return

        def send():
            from mailer.mailer_core import MailerCore
            try:
                core = MailerCore(config_path="config.ini")
                core._send_test_emails(recipients.split(","))
                self._status_var.set("Test mail sent!")
            except Exception as e:
                self._status_var.set(f"Test failed: {e}")

        threading.Thread(target=send, daemon=True).start()
        self._status_var.set("Sending test...")

    def _poll_stats(self):
        s = db_stats()
        total = s["total"]
        sent = s["SENT"]
        failed = s["FAILED"]
        pending = s["PENDING"] + s["IN_PROGRESS"]
        processed = sent + failed

        self._lbl["Total"].config(text=str(total))
        self._lbl["Sent"].config(text=str(sent))
        self._lbl["Failed"].config(text=str(failed))
        self._lbl["Pending"].config(text=str(pending))

        if total > 0:
            self._progress["value"] = processed / total * 100
        elapsed = time.time() - self._started_at if self._running else 0
        speed = processed / elapsed if elapsed > 1 else 0
        self._lbl["Speed"].config(text=f"{speed:.1f}/s")
        remaining = (total - processed) / speed if speed > 0 else 0
        m, sec = divmod(int(remaining), 60)
        h, m = divmod(m, 60)
        self._lbl["ETA"].config(text=f"{h}:{m:02d}:{sec:02d}" if speed > 0 else "--:--")

        if not self._running and self._btn_start["state"] == "disabled":
            self._btn_start.config(state="normal")
            self._btn_pause.config(state="disabled")
            self._btn_stop.config(state="disabled")
            if self._status_var.get() == "Running":
                self._status_var.set("Finished")

        self.after(2000, self._poll_stats)
