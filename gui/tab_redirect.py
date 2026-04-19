import os
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, messagebox

REDIRECT_DB = "redirects.db"


class RedirectTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._generating = False
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        gen_frame = ttk.LabelFrame(self, text="Generate Redirects", padding=10)
        gen_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(gen_frame, text="Target URL:").grid(row=0, column=0, sticky="w")
        self._url_var = tk.StringVar()
        ttk.Entry(gen_frame, textvariable=self._url_var, width=50).grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(gen_frame, text="Count:").grid(row=1, column=0, sticky="w")
        self._count_var = tk.IntVar(value=100)
        ttk.Spinbox(gen_frame, from_=1, to=1000, textvariable=self._count_var, width=10).grid(row=1, column=1, sticky="w", padx=5, pady=2)

        self._gen_btn = ttk.Button(gen_frame, text="Generate", command=self._generate)
        self._gen_btn.grid(row=0, column=2, rowspan=2, padx=10)

        self._gen_progress = ttk.Progressbar(gen_frame, length=300, mode="determinate")
        self._gen_progress.grid(row=2, column=0, columnspan=3, pady=5, sticky="ew")
        self._gen_status = tk.StringVar(value="")
        ttk.Label(gen_frame, textvariable=self._gen_status).grid(row=3, column=0, columnspan=3)

        manual_frame = ttk.LabelFrame(self, text="Add Manually", padding=10)
        manual_frame.pack(fill="x", padx=10, pady=5)

        self._manual_var = tk.StringVar()
        ttk.Entry(manual_frame, textvariable=self._manual_var, width=60).pack(side="left", padx=5)
        ttk.Button(manual_frame, text="Add", command=self._add_manual).pack(side="left", padx=5)
        ttk.Button(manual_frame, text="Bulk Add", command=self._bulk_add).pack(side="left", padx=5)

        list_frame = ttk.LabelFrame(self, text="Redirect Pool", padding=10)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        cols = ("url", "created")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=12)
        self._tree.heading("url", text="Short URL")
        self._tree.heading("created", text="Created")
        self._tree.column("url", width=400)
        self._tree.column("created", width=150)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        self._tree.config(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=5)
        self._count_label = tk.StringVar(value="0 links")
        ttk.Label(bf, textvariable=self._count_label, font=("", 10, "bold")).pack(side="left")
        ttk.Button(bf, text="Refresh", command=self._refresh_list).pack(side="left", padx=10)
        ttk.Button(bf, text="Delete Selected", command=self._delete_selected).pack(side="left", padx=5)
        ttk.Button(bf, text="Clear All", command=self._clear_all).pack(side="left", padx=5)

    def _ensure_db(self):
        conn = sqlite3.connect(REDIRECT_DB, timeout=10)
        conn.execute("""CREATE TABLE IF NOT EXISTS redirect_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_url TEXT NOT NULL,
            target_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        return conn

    def _refresh_list(self):
        for item in self._tree.get_children():
            self._tree.delete(item)
        conn = self._ensure_db()
        rows = conn.execute("SELECT short_url, created_at FROM redirect_links ORDER BY id DESC").fetchall()
        conn.close()
        for url, created in rows:
            self._tree.insert("", "end", values=(url, created or ""))
        self._count_label.set(f"{len(rows)} links")

    def _add_manual(self):
        url = self._manual_var.get().strip()
        if not url:
            return
        conn = self._ensure_db()
        conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)", (url, "manual"))
        conn.commit()
        conn.close()
        self._manual_var.set("")
        self._refresh_list()

    def _bulk_add(self):
        win = tk.Toplevel(self)
        win.title("Bulk Add Redirects")
        win.geometry("500x300")
        ttk.Label(win, text="One URL per line:").pack(anchor="w", padx=10, pady=5)
        txt = tk.Text(win, height=12, font=("Consolas", 10))
        txt.pack(fill="both", expand=True, padx=10)

        def do_add():
            lines = txt.get("1.0", "end").strip().splitlines()
            conn = self._ensure_db()
            added = 0
            for ln in lines:
                ln = ln.strip()
                if ln:
                    conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)", (ln, "manual"))
                    added += 1
            conn.commit()
            conn.close()
            win.destroy()
            self._refresh_list()
            messagebox.showinfo("Done", f"Added {added} links")

        ttk.Button(win, text="Add All", command=do_add).pack(pady=5)

    def _delete_selected(self):
        sel = self._tree.selection()
        if not sel:
            return
        conn = self._ensure_db()
        for item in sel:
            url = self._tree.item(item, "values")[0]
            conn.execute("DELETE FROM redirect_links WHERE short_url = ?", (url,))
        conn.commit()
        conn.close()
        self._refresh_list()

    def _clear_all(self):
        if messagebox.askyesno("Clear", "Delete ALL redirect links?"):
            conn = self._ensure_db()
            conn.execute("DELETE FROM redirect_links")
            conn.commit()
            conn.close()
            self._refresh_list()

    def _generate(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning("URL", "Enter a target URL")
            return
        if self._generating:
            return
        count = self._count_var.get()
        self._generating = True
        self._gen_btn.config(state="disabled")
        self._gen_progress["value"] = 0
        self._gen_progress["maximum"] = count

        def worker():
            from mailer.redirect_manager import RedirectManager
            mgr = RedirectManager(target_url=url, db_path=REDIRECT_DB, enabled=True)
            success = 0
            for i in range(count):
                result = mgr._generate_one()
                if result:
                    with sqlite3.connect(REDIRECT_DB, timeout=10) as conn:
                        conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)", (result, url))
                    success += 1
                self._gen_progress["value"] = i + 1
                self._gen_status.set(f"{i+1}/{count} ({success} success)")
            self._generating = False
            self._gen_btn.config(state="normal")
            self._gen_status.set(f"Done: {success}/{count} generated")
            self._refresh_list()

        threading.Thread(target=worker, daemon=True).start()
