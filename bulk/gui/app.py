"""Bulk Mailer GUI — PySide6 Main Window."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtWidgets import (QApplication, QMainWindow, QTabWidget,
                                 QStatusBar, QMessageBox)
from PySide6.QtCore import Qt

from bulk.mailer.db_manager import BulkDBManager

from .tab_brands import BrandsTab
from .tab_smtp import SMTPTab
from .tab_lists import ListsTab
from .tab_macros import MacrosTab
from .tab_composer import ComposerTab
from .tab_mailing import MailingTab
from .tab_preview import PreviewTab
from .tab_cloudflare import CloudflareTab
from .tab_macro_help import MacroHelpTab
from .tab_logs import LogsTab


class BulkMailerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bulk Mailer Control Panel")
        self.setMinimumSize(1200, 750)
        self.db = BulkDBManager("bulk.db")

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        tabs.addTab(MailingTab(self.db), "Mailing")
        tabs.addTab(BrandsTab(self.db), "Brands")
        tabs.addTab(SMTPTab(self.db), "SMTP")
        tabs.addTab(ListsTab(self.db), "Lists")
        tabs.addTab(ComposerTab(self.db), "Composer")
        tabs.addTab(MacrosTab(self.db), "Macros")
        tabs.addTab(MacroHelpTab(), "Macro Help")
        tabs.addTab(PreviewTab(self.db), "Preview")
        tabs.addTab(CloudflareTab(self.db), "Cloudflare")
        tabs.addTab(LogsTab(self.db), "Logs")

        self.statusBar().showMessage("Ready")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = BulkMailerWindow()
    win.show()
    sys.exit(app.exec())
