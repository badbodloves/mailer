import os
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .helpers import read_config, scan_files, open_html_in_browser, open_raw_in_browser

HTML_DIR = "html_bodies"


class EditorTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=5)

        ttk.Label(toolbar, text="Template:").pack(side="left")
        self._file_var = tk.StringVar()
        self._file_cb = ttk.Combobox(toolbar, textvariable=self._file_var, state="readonly", width=30)
        self._file_cb.pack(side="left", padx=5)
        self._file_cb.bind("<<ComboboxSelected>>", self._load_file)

        ttk.Button(toolbar, text="Refresh", command=self._refresh).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Save", command=self._save).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Save As...", command=self._save_as).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Preview HTML", command=self._preview_html).pack(side="left", padx=5)
        ttk.Button(toolbar, text="Preview + AntiFingerprint", command=self._preview_af).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Show Raw MIME", command=self._preview_raw).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Diff Before/After", command=self._show_diff).pack(side="left", padx=2)

        self._editor = tk.Text(self, font=("Consolas", 11), wrap="none", undo=True)
        sy = ttk.Scrollbar(self, orient="vertical", command=self._editor.yview)
        sx = ttk.Scrollbar(self, orient="horizontal", command=self._editor.xview)
        self._editor.config(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self._editor.pack(fill="both", expand=True, padx=10)

        self._refresh()

    def _refresh(self):
        os.makedirs(HTML_DIR, exist_ok=True)
        files = scan_files(HTML_DIR, (".html", ".htm"))
        self._file_cb["values"] = files
        if files and not self._file_var.get():
            self._file_var.set(files[0])
            self._load_file()

    def _load_file(self, event=None):
        name = self._file_var.get()
        if not name:
            return
        path = os.path.join(HTML_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self._editor.delete("1.0", "end")
            self._editor.insert("1.0", content)
        except OSError as e:
            messagebox.showerror("Error", str(e))

    def _save(self):
        name = self._file_var.get()
        if not name:
            self._save_as()
            return
        path = os.path.join(HTML_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._editor.get("1.0", "end-1c"))
        messagebox.showinfo("Saved", f"Saved to {path}")

    def _save_as(self):
        path = filedialog.asksaveasfilename(
            initialdir=HTML_DIR, defaultextension=".html",
            filetypes=[("HTML", "*.html"), ("All", "*.*")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._editor.get("1.0", "end-1c"))
            self._refresh()

    def _get_processed(self, with_af=False):
        from mailer.content_engine import ContentEngine
        cp = read_config()
        ce = ContentEngine(
            html_dir=HTML_DIR, attachments_dir="",
            spintax_dir=cp.get("paths", "spintax_dir", fallback="spintaxes"),
            names_file=cp.get("paths", "names_file", fallback=""),
            subjects_file=cp.get("paths", "subjects_file", fallback=""),
        )
        raw_html = self._editor.get("1.0", "end-1c")
        processed = ce.process(raw_html, "preview@example.com")
        if with_af:
            from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
            af = AdvancedAntiFingerprintEngine(enable_classes=True, structure_variation=0.5)
            processed = af.transform(processed)
        return raw_html, processed

    def _preview_html(self):
        try:
            _, html = self._get_processed(with_af=False)
            open_html_in_browser(html)
        except Exception as e:
            messagebox.showerror("Preview Error", str(e))

    def _preview_af(self):
        try:
            _, html = self._get_processed(with_af=True)
            open_html_in_browser(html)
        except Exception as e:
            messagebox.showerror("Preview Error", str(e))

    def _preview_raw(self):
        try:
            _, html = self._get_processed(with_af=True)
            from mailer.content_engine import ContentEngine
            from mailer.mime_builder import MIMEBuilder
            cp = read_config()
            plain = ContentEngine.html_to_plaintext(html)
            from_email = cp.get("sender", "from_email", fallback="") or "noreply@example.com"
            raw = MIMEBuilder.build_email(
                from_name="Preview Sender", from_email=from_email,
                to_email="preview@example.com",
                subject="Preview Subject", html_body=html, plain_body=plain,
            )
            open_raw_in_browser(raw)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _show_diff(self):
        try:
            before, after = self._get_processed(with_af=True)
            diff_html = (
                "<html><head><style>"
                "body{font-family:monospace;font-size:13px;display:flex;gap:20px;padding:20px}"
                ".col{flex:1;border:1px solid #ccc;padding:15px;overflow-x:auto}"
                "h3{margin-top:0;color:#555}"
                "pre{white-space:pre-wrap;word-wrap:break-word}"
                "</style></head><body>"
                "<div class='col'><h3>BEFORE (Spintax only)</h3>"
                f"<pre>{_esc(before)}</pre></div>"
                "<div class='col'><h3>AFTER (+ AntiFingerprint)</h3>"
                f"<pre>{_esc(after)}</pre></div>"
                "</body></html>"
            )
            open_html_in_browser(diff_html)
        except Exception as e:
            messagebox.showerror("Error", str(e))


def _esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
