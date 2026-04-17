import logging
import sys
import time
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config_manager import ConfigManager
from .db_manager import DBManager
from .content_engine import ContentEngine
from .mime_builder import MIMEBuilder
from .smtp_worker import SMTPPool, SMTPWorker
from .ui_console import UIConsole

try:
    from colorama import Fore, Style
except ImportError:
    class _D:
        def __getattr__(self, _):
            return ""
    Fore = _D()
    Style = _D()


class MailerCore:
    BATCH_SIZE = 200

    def __init__(self, config_path: str = "config.ini"):
        self._shutdown = threading.Event()
        self._config = ConfigManager(config_path)
        self._setup_logging()

        self._db = DBManager(self._config.db_path)
        self._content = ContentEngine(
            html_dir=self._config.html_dir,
            attachments_dir=self._config.attachments_dir,
            spintax_dir=self._config.spintax_dir,
            names_file=self._config.names_file,
            subjects_file=self._config.subjects_file,
        )
        self._smtp_pool = SMTPPool(
            self._config.smtp_file,
            timeout=self._config.smtp_timeout,
            warmup_delay=self._config.warmup_delay,
            warmup_count=self._config.warmup_count,
        )
        self._worker = SMTPWorker(
            self._smtp_pool,
            normal_delay=self._config.normal_delay,
            provider_delay=self._config.provider_delay,
        )
        self._ui = UIConsole()

        signal.signal(signal.SIGINT, self._handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_signal)

    @staticmethod
    def _setup_logging() -> None:
        handler = logging.FileHandler("smtp_errors.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        log = logging.getLogger("mailer.smtp")
        log.setLevel(logging.ERROR)
        log.addHandler(handler)

    def _handle_signal(self, signum, frame) -> None:
        print(f"\n{Fore.YELLOW}[!] Shutdown signal received. Finishing current batch...{Style.RESET_ALL}")
        self._shutdown.set()

    def run(self) -> None:
        self._print_banner()

        if self._smtp_pool.total == 0:
            print(f"{Fore.RED}[!] No SMTP accounts loaded. Check {self._config.smtp_file}{Style.RESET_ALL}")
            sys.exit(1)
        print(f"  SMTP accounts: {Fore.GREEN}{self._smtp_pool.size}{Style.RESET_ALL} live / {self._smtp_pool.total} total")

        if self._content.has_names:
            print(f"  Names pool:    {Fore.GREEN}loaded{Style.RESET_ALL}")
        if self._content.has_subjects:
            print(f"  Subjects pool: {Fore.GREEN}loaded{Style.RESET_ALL}")

        self._db.reset_in_progress()
        loaded = self._db.load_leads(self._config.leads_file)
        counts = self._db.count_by_state()
        total_pending = counts.get(DBManager.STATE_PENDING, 0)
        total_all = self._db.total_count()
        print(f"  Leads DB: {total_all} total, {Fore.CYAN}{total_pending} pending{Style.RESET_ALL}, {counts.get(DBManager.STATE_SENT, 0)} sent, {counts.get(DBManager.STATE_FAILED, 0)} failed")
        if loaded:
            print(f"  New leads loaded from file: {loaded}")

        if total_pending == 0:
            print(f"\n{Fore.YELLOW}[*] No pending leads to process.{Style.RESET_ALL}")
            return

        thread_count = min(self._config.thread_count, self._smtp_pool.size * 5)
        thread_count = max(thread_count, 1)
        print(f"  Threads: {Fore.GREEN}{thread_count}{Style.RESET_ALL}")

        test_recipients = self._config.test_recipients
        if test_recipients:
            print(f"\n{Fore.CYAN}[*] Sending test emails to {len(test_recipients)} recipients...{Style.RESET_ALL}")
            self._send_test_emails(test_recipients)

        print(f"\n{Fore.GREEN}[>] Starting mass send...{Style.RESET_ALL}\n")
        self._ui.start(total_pending)

        try:
            self._process_loop(thread_count)
        finally:
            self._ui.stop()
            self._ui.print_summary()
            self._db.reset_in_progress()
            self._db.close()

    def _process_loop(self, thread_count: int) -> None:
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            while not self._shutdown.is_set():
                batch = self._db.fetch_pending_batch(self.BATCH_SIZE)
                if not batch:
                    break

                lead_ids = [row[0] for row in batch]
                self._db.mark_in_progress(lead_ids)

                futures = {}
                for lead_id, email in batch:
                    if self._shutdown.is_set():
                        break
                    future = executor.submit(self._send_one, lead_id, email)
                    futures[future] = (lead_id, email)

                for future in as_completed(futures):
                    if self._shutdown.is_set():
                        break
                    lead_id, email = futures[future]
                    try:
                        success = future.result(timeout=120)
                        if success:
                            self._db.mark_sent(lead_id)
                            self._ui.record_sent()
                        else:
                            self._db.mark_failed(lead_id, "send returned False")
                            self._ui.record_failed()
                    except Exception as exc:
                        self._db.mark_failed(lead_id, str(exc)[:500])
                        self._ui.record_failed()

    def _send_one(self, lead_id: int, email: str) -> bool:
        if self._shutdown.is_set():
            return False

        account = self._smtp_pool.acquire()
        if account is None:
            return False

        from_email = self._config.from_email
        if not from_email:
            from_email = account.user

        from_name = self._content.process(self._config.from_name, email)

        if self._content.has_subjects:
            subject_template = self._content.get_random_subject()
        else:
            subject_template = self._config.subject
        subject = self._content.process(subject_template, email)

        html_template = self._content.get_random_html()
        if html_template is None:
            html_template = "<p>Hello {email_user},</p><p>This is your notification.</p>"

        html_body = self._content.process(html_template, email)
        plain_body = ContentEngine.html_to_plaintext(html_body)

        attachment = self._content.get_random_attachment()

        raw_msg = MIMEBuilder.build_email(
            from_name=from_name,
            from_email=from_email,
            to_email=email,
            subject=subject,
            html_body=html_body,
            plain_body=plain_body,
            attachment=attachment,
        )

        success = self._worker.send(from_email, email, raw_msg, account=account)

        delay = self._worker.get_delay(email)
        if delay > 0 and not self._shutdown.is_set():
            time.sleep(delay)

        return success

    def _send_test_emails(self, recipients: list) -> None:
        for recipient in recipients:
            recipient = recipient.strip()
            if not recipient:
                continue
            try:
                success = self._send_one(-1, recipient)
                if success:
                    print(f"    {Fore.GREEN}[OK]{Style.RESET_ALL} Test sent to {recipient}")
                else:
                    print(f"    {Fore.RED}[FAIL]{Style.RESET_ALL} Test to {recipient}")
            except Exception as exc:
                print(f"    {Fore.RED}[FAIL]{Style.RESET_ALL} Test to {recipient}: {exc}")

    @staticmethod
    def _print_banner() -> None:
        print(f"""
{Fore.CYAN}╔══════════════════════════════════════════════════╗
║           MASS MAILER v1.0                       ║
║           High-Performance Email Engine           ║
╚══════════════════════════════════════════════════╝{Style.RESET_ALL}
""")
