import os
import tkinter as tk
from tkinter import ttk, messagebox

from .helpers import read_config, save_config

SPINTAX_DIR = "spintaxes"


class ConfigTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._entries = {}
        self._build_ui()

    def _build_ui(self):
        canvas = tk.Canvas(self)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        self._inner = scroll_frame

        cp = read_config()
        row = 0
        for section in cp.sections():
            lf = ttk.LabelFrame(scroll_frame, text=f"[{section}]", padding=8)
            lf.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10, pady=5)
            row += 1
            for i, (key, val) in enumerate(cp.items(section)):
                ttk.Label(lf, text=key, width=25, anchor="w").grid(row=i, column=0, sticky="w", padx=5, pady=1)
                var = tk.StringVar(value=val)
                entry = ttk.Entry(lf, textvariable=var, width=50)
                entry.grid(row=i, column=1, sticky="ew", padx=5, pady=1)
                self._entries[(section, key)] = var

        btn_frame = ttk.Frame(scroll_frame)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="Save Config", command=self._save).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Reload", command=self._reload).pack(side="left", padx=5)
        row += 1

        # Spintax editor
        sf = ttk.LabelFrame(scroll_frame, text="Spintax Files", padding=8)
        sf.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10, pady=5)

        top = ttk.Frame(sf)
        top.pack(fill="x")
        ttk.Label(top, text="File:").pack(side="left")
        self._spin_var = tk.StringVar()
        self._spin_cb = ttk.Combobox(top, textvariable=self._spin_var, state="readonly", width=25)
        self._spin_cb.pack(side="left", padx=5)
        self._spin_cb.bind("<<ComboboxSelected>>", self._load_spintax)
        ttk.Button(top, text="Refresh", command=self._refresh_spintax).pack(side="left", padx=2)
        ttk.Button(top, text="Save", command=self._save_spintax).pack(side="left", padx=2)
        ttk.Button(top, text="New...", command=self._new_spintax).pack(side="left", padx=2)

        self._spin_editor = tk.Text(sf, height=10, font=("Consolas", 10), wrap="word")
        self._spin_editor.pack(fill="both", expand=True, pady=5)
        self._refresh_spintax()

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

    def _save(self):
        cp = read_config()
        for (sec, key), var in self._entries.items():
            if not cp.has_section(sec):
                cp.add_section(sec)
            cp.set(sec, key, var.get())
        save_config(cp)
        messagebox.showinfo("Config", "Saved!")

    def _reload(self):
        cp = read_config()
        for (sec, key), var in self._entries.items():
            try:
                var.set(cp.get(sec, key, fallback=""))
            except Exception:
                pass

    def _refresh_spintax(self):
        os.makedirs(SPINTAX_DIR, exist_ok=True)
        files = sorted(f for f in os.listdir(SPINTAX_DIR) if f.endswith(".txt"))
        self._spin_cb["values"] = files
        if files and not self._spin_var.get():
            self._spin_var.set(files[0])
            self._load_spintax()

    def _load_spintax(self, event=None):
        name = self._spin_var.get()
        if not name:
            return
        path = os.path.join(SPINTAX_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._spin_editor.delete("1.0", "end")
                self._spin_editor.insert("1.0", f.read())
        except OSError:
            pass

    def _save_spintax(self):
        name = self._spin_var.get()
        if not name:
            return
        path = os.path.join(SPINTAX_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._spin_editor.get("1.0", "end-1c"))
        messagebox.showinfo("Spintax", f"Saved {name}")

    def _new_spintax(self):
        win = tk.Toplevel(self)
        win.title("New Spintax File")
        win.geometry("300x100")
        ttk.Label(win, text="Filename (without .txt):").pack(pady=5)
        var = tk.StringVar()
        ttk.Entry(win, textvariable=var, width=30).pack()

        def create():
            name = var.get().strip()
            if name:
                path = os.path.join(SPINTAX_DIR, f"{name}.txt")
                open(path, "w").close()
                win.destroy()
                self._refresh_spintax()
                self._spin_var.set(f"{name}.txt")

        ttk.Button(win, text="Create", command=create).pack(pady=5)
