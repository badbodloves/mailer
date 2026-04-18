import smtplib
import ssl
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .dns_cache import DNSCache
from .utils import resolve_txt_paths

try:
    import certifi
    HAS_CERTIFI = True
except ImportError:
    HAS_CERTIFI = False

logger = logging.getLogger("mailer.smtp")

STRICT_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk",
    "yahoo.fr", "yahoo.de", "yahoo.it", "yahoo.es",
    "aol.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com",
    "mail.ru", "yandex.ru", "yandex.com",
})


@dataclass
class SMTPAccount:
    host: str
    port: int
    user: str
    password: str
    send_count: int = 0
    dead: bool = False
    last_used: float = 0.0
    warmup_done: bool = False
    _warmup_sends: int = field(default=0, repr=False)


class SMTPPool:
    def __init__(
        self,
        smtp_path: str,
        timeout: int = 30,
        warmup_delay: float = 30.0,
        warmup_count: int = 5,
        ignore_ssl_errors: bool = True,
    ):
        self._accounts: List[SMTPAccount] = []
        self._lock = threading.Lock()
        self._index = 0
        self._timeout = timeout
        self._warmup_delay = warmup_delay
        self._warmup_count = warmup_count
        self._ignore_ssl_errors = ignore_ssl_errors
        self._dns_cache = DNSCache()
        self._load(smtp_path)

    def _load(self, path: str) -> None:
        for fpath in resolve_txt_paths(path):
            self._parse_smtp_file(fpath)

    def _parse_smtp_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(",")
                    if len(parts) < 4:
                        continue
                    host = parts[0].strip()
                    try:
                        port = int(parts[1].strip())
                    except ValueError:
                        continue
                    user = parts[2].strip()
                    password = parts[3].strip()
                    self._accounts.append(SMTPAccount(host, port, user, password))
        except OSError:
            pass

    @property
    def size(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if not a.dead)

    @property
    def total(self) -> int:
        return len(self._accounts)

    def acquire(self) -> Optional[SMTPAccount]:
        with self._lock:
            alive = [a for a in self._accounts if not a.dead]
            if not alive:
                return None
            account = alive[self._index % len(alive)]
            self._index += 1
            return account

    def mark_dead(self, account: SMTPAccount, reason: str = "") -> None:
        with self._lock:
            account.dead = True
        logger.error("SMTP dead: %s@%s - %s", account.user, account.host, reason)

    def record_send(self, account: SMTPAccount) -> None:
        with self._lock:
            account.send_count += 1
            account.last_used = time.monotonic()
            if not account.warmup_done:
                account._warmup_sends += 1
                if account._warmup_sends >= self._warmup_count:
                    account.warmup_done = True

    def get_warmup_delay(self, account: SMTPAccount) -> float:
        with self._lock:
            if not account.warmup_done:
                return self._warmup_delay
        return 0.0

    def _build_ssl_context(self) -> ssl.SSLContext:
        """Build an SSL context.

        - ignore_ssl_errors=True -> permissive context: no hostname check,
          no certificate verification (CERT_NONE). No handshake errors
          for self-signed / non-conformant server certificates.
        - ignore_ssl_errors=False -> strict validation using certifi's CA
          bundle (if installed) or the system trust store as fallback.
        """
        if self._ignore_ssl_errors:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        if HAS_CERTIFI:
            return ssl.create_default_context(cafile=certifi.where())
        return ssl.create_default_context()

    def connect(self, account: SMTPAccount) -> smtplib.SMTP:
        self._dns_cache.resolve_a(account.host)
        ctx = self._build_ssl_context()

        if account.port == 465:
            server = smtplib.SMTP_SSL(
                account.host, account.port,
                timeout=self._timeout, context=ctx,
            )
            server.ehlo()
        else:
            server = smtplib.SMTP(account.host, account.port, timeout=self._timeout)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=ctx)
                server.ehlo()

        server.login(account.user, account.password)
        return server


class SMTPWorker:
    def __init__(
        self,
        pool: SMTPPool,
        normal_delay: float = 0.3,
        provider_delay: float = 6.0,
    ):
        self._pool = pool
        self._normal_delay = normal_delay
        self._provider_delay = provider_delay

    def send(
        self,
        from_email: str,
        to_email: str,
        raw_message: str,
        account: Optional[SMTPAccount] = None,
    ) -> bool:
        if account is None:
            account = self._pool.acquire()
        if account is None:
            raise RuntimeError("No live SMTP servers available")

        warmup_wait = self._pool.get_warmup_delay(account)
        if warmup_wait > 0:
            time.sleep(warmup_wait)

        server: Optional[smtplib.SMTP] = None
        try:
            server = self._pool.connect(account)
            server.sendmail(from_email, [to_email], raw_message)
            self._pool.record_send(account)
            return True
        except smtplib.SMTPAuthenticationError as exc:
            self._pool.mark_dead(account, f"Auth failed: {exc}")
            logger.error("AUTH FAIL %s: %s", account.user, exc)
            return False
        except ssl.SSLError as exc:
            logger.error("SSL error %s@%s: %s", account.user, account.host, exc)
            return False
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("SMTP error %s: %s", account.user, exc)
            return False
        finally:
            if server:
                try:
                    server.quit()
                except Exception:
                    pass

    def get_delay(self, to_email: str) -> float:
        domain = to_email.split("@")[1].lower() if "@" in to_email else ""
        if domain in STRICT_PROVIDERS:
            return self._provider_delay
        return self._normal_delay
