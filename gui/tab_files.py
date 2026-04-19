import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .helpers import scan_files

LEADS_DIR = "Leads"
SMTPS_DIR = "SMTPs"
LOGOS_DIR = "logos"

for _d in (LEADS_DIR, SMTPS_DIR, LOGOS_DIR):
    os.makedirs(_d, exist_ok=True)


class FilesTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=5)

        # Leads
        lf_leads = ttk.LabelFrame(paned, text="Leads", padding=5)
        paned.add(lf_leads, weight=1)
        self._leads_list = tk.Listbox(lf_leads, height=15)
        self._leads_list.pack(fill="both", expand=True)
        bf1 = ttk.Frame(lf_leads)
        bf1.pack(fill="x", pady=3)
        ttk.Button(bf1, text="Add", command=lambda: self._add(LEADS_DIR, self._leads_list, ".txt")).pack(side="left", padx=2)
        ttk.Button(bf1, text="Delete", command=lambda: self._delete(LEADS_DIR, self._leads_list)).pack(side="left", padx=2)
        ttk.Button(bf1, text="View", command=lambda: self._view(LEADS_DIR, self._leads_list)).pack(side="left", padx=2)

        # SMTPs
        lf_smtp = ttk.LabelFrame(paned, text="SMTPs", padding=5)
        paned.add(lf_smtp, weight=1)
        self._smtp_list = tk.Listbox(lf_smtp, height=15)
        self._smtp_list.pack(fill="both", expand=True)
        bf2 = ttk.Frame(lf_smtp)
        bf2.pack(fill="x", pady=3)
        ttk.Button(bf2, text="Add", command=lambda: self._add(SMTPS_DIR, self._smtp_list, ".txt")).pack(side="left", padx=2)
        ttk.Button(bf2, text="Delete", command=lambda: self._delete(SMTPS_DIR, self._smtp_list)).pack(side="left", padx=2)
        ttk.Button(bf2, text="View", command=lambda: self._view(SMTPS_DIR, self._smtp_list)).pack(side="left", padx=2)

        # Logos
        lf_logos = ttk.LabelFrame(paned, text="Logos", padding=5)
        paned.add(lf_logos, weight=1)
        self._logo_list = tk.Listbox(lf_logos, height=15)
        self._logo_list.pack(fill="both", expand=True)
        bf3 = ttk.Frame(lf_logos)
        bf3.pack(fill="x", pady=3)
        ttk.Button(bf3, text="Add", command=lambda: self._add(LOGOS_DIR, self._logo_list, (".png", ".jpg", ".jpeg"))).pack(side="left", padx=2)
        ttk.Button(bf3, text="Delete", command=lambda: self._delete(LOGOS_DIR, self._logo_list)).pack(side="left", padx=2)

        self._logo_preview = ttk.Label(lf_logos, text="(select to preview)")
        self._logo_preview.pack(pady=5)
        self._logo_list.bind("<<ListboxSelect>>", self._preview_logo)

        self._refresh_all()

    def _refresh_all(self):
        self._refresh_list(LEADS_DIR, self._leads_list, (".txt",))
        self._refresh_list(SMTPS_DIR, self._smtp_list, (".txt",))
        self._refresh_list(LOGOS_DIR, self._logo_list, (".png", ".jpg", ".jpeg", ".gif", ".webp"))

    def _refresh_list(self, folder, listbox, exts):
        listbox.delete(0, "end")
        for f in scan_files(folder, exts):
            size = os.path.getsize(os.path.join(folder, f))
            listbox.insert("end", f"{f}  ({size:,} bytes)")

    def _add(self, folder, listbox, exts):
        if isinstance(exts, str):
            exts = (exts,)
        ftypes = [("Files", " ".join(f"*{e}" for e in exts))]
        paths = filedialog.askopenfilenames(filetypes=ftypes)
        for src in paths:
            name = os.path.basename(src)
            dest = os.path.join(folder, name)
            with open(src, "rb") as fi, open(dest, "wb") as fo:
                fo.write(fi.read())
        self._refresh_all()

    def _delete(self, folder, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        entry = listbox.get(sel[0])
        name = entry.split("  (")[0]
        if messagebox.askyesno("Delete", f"Delete {name}?"):
            os.unlink(os.path.join(folder, name))
            self._refresh_all()

    def _view(self, folder, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        entry = listbox.get(sel[0])
        name = entry.split("  (")[0]
        path = os.path.join(folder, name)
        win = tk.Toplevel(self)
        win.title(name)
        win.geometry("600x400")
        txt = tk.Text(win, font=("Consolas", 10), wrap="word")
        txt.pack(fill="both", expand=True)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                txt.insert("1.0", f.read())
        except OSError:
            txt.insert("1.0", "(cannot read)")

    def _preview_logo(self, event=None):
        sel = self._logo_list.curselection()
        if not sel:
            return
        entry = self._logo_list.get(sel[0])
        name = entry.split("  (")[0]
        path = os.path.join(LOGOS_DIR, name)
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            img.thumbnail((200, 150))
            photo = ImageTk.PhotoImage(img)
            self._logo_preview.config(image=photo, text="")
            self._logo_preview._photo = photo
        except Exception:
            self._logo_preview.config(text=f"(cannot preview {name})", image="")
