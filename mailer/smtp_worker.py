import smtplib
import ssl
import time
import random
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

try:
    import socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

logger = logging.getLogger("mailer.smtp")

STRICT_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk",
    "yahoo.fr", "yahoo.de", "yahoo.it", "yahoo.es",
    "aol.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com",
    "mail.ru", "yandex.ru", "yandex.com",
})

FATAL_RECIPIENT_CODES = frozenset({550, 551, 552, 553, 554, 555})

BACKOFF_BASE = 100


@dataclass
class SendResult:
    outcome: str
    error: str = ""
    smtp_code: int = 0

    SUCCESS = "success"
    FATAL = "fatal"
    TRANSIENT = "transient"

    @property
    def is_success(self) -> bool:
        return self.outcome == self.SUCCESS

    @property
    def is_fatal(self) -> bool:
        return self.outcome == self.FATAL

    @property
    def is_transient(self) -> bool:
        return self.outcome == self.TRANSIENT


@dataclass
class ProxyConfig:
    host: str
    port: int
    username: str = ""
    password: str = ""

    @staticmethod
    def parse(text: str) -> Optional["ProxyConfig"]:
        text = text.strip()
        if not text:
            return None
        text = text.replace("socks5://", "").replace("socks://", "")
        user, pwd = "", ""
        if "@" in text:
            auth, text = text.rsplit("@", 1)
            if ":" in auth:
                user, pwd = auth.split(":", 1)
        parts = text.split(":")
        if len(parts) < 2:
            return None
        if len(parts) == 4:
            return ProxyConfig(parts[0], int(parts[1]), parts[2], parts[3])
        return ProxyConfig(parts[0], int(parts[1]), user, pwd)


@dataclass
class SMTPAccount:
    host: str
    port: int
    user: str
    password: str
    proxy: Optional[ProxyConfig] = None
    send_count: int = 0
    dead: bool = False
    last_used: float = 0.0
    warmup_done: bool = False
    fail_count: int = 0
    suspended_until: float = 0.0
    _warmup_sends: int = field(default=0, repr=False)

    @property
    def is_suspended(self) -> bool:
        return not self.dead and self.suspended_until > time.monotonic()

    @property
    def is_available(self) -> bool:
        return not self.dead and not self.is_suspended

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}:{self.user}"


class SMTPPool:
    def __init__(
        self,
        smtp_path: str,
        timeout: int = 30,
        warmup_delay: float = 30.0,
        warmup_count: int = 5,
        ignore_ssl_errors: bool = True,
        proxy_file: str = "",
        proxy_rotate_every: int = 0,
    ):
        self._accounts: List[SMTPAccount] = []
        self._lock = threading.Lock()
        self._index = 0
        self._timeout = timeout
        self._warmup_delay = warmup_delay
        self._warmup_count = warmup_count
        self._ignore_ssl_errors = ignore_ssl_errors
        self._dns_cache = DNSCache()
        self._proxies: List[ProxyConfig] = []
        self._proxy_index = 0
        self._proxy_rotate_every = proxy_rotate_every
        self._proxy_send_count = 0
        self._load(smtp_path)
        self._load_proxies(proxy_file)

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
                    proxy = ProxyConfig.parse(parts[4]) if len(parts) >= 5 else None
                    self._accounts.append(SMTPAccount(host, port, user, password, proxy=proxy))
        except OSError:
            pass

    def _load_proxies(self, path: str) -> None:
        if not path:
            return
        for fpath in resolve_txt_paths(path):
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        p = ProxyConfig.parse(line)
                        if p:
                            self._proxies.append(p)
            except OSError:
                pass

    def get_current_proxy(self) -> Optional[ProxyConfig]:
        with self._lock:
            if not self._proxies:
                return None
            if self._proxy_rotate_every > 0:
                self._proxy_send_count += 1
                if self._proxy_send_count >= self._proxy_rotate_every:
                    self._proxy_send_count = 0
                    self._proxy_index = (self._proxy_index + 1) % len(self._proxies)
            return self._proxies[self._proxy_index % len(self._proxies)]

    @property
    def proxy_count(self) -> int:
        return len(self._proxies)

    @property
    def total(self) -> int:
        return len(self._accounts)

    @property
    def size(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if not a.dead)

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if a.is_available)

    @property
    def all_dead(self) -> bool:
        with self._lock:
            return all(a.dead for a in self._accounts)

    def acquire(self) -> Optional[SMTPAccount]:
        with self._lock:
            available = [a for a in self._accounts if a.is_available]
            if not available:
                return None
            return random.choice(available)

    def suspend(self, account: SMTPAccount, reason: str = "") -> None:
        with self._lock:
            account.fail_count += 1
            cooldown = BACKOFF_BASE * account.fail_count
            account.suspended_until = time.monotonic() + cooldown
        logger.error(
            "SMTP suspended: %s (%ds cooldown, fail #%d) - %s",
            account.key, cooldown, account.fail_count, reason,
        )

    def mark_dead(self, account: SMTPAccount, reason: str = "") -> None:
        with self._lock:
            account.dead = True
        logger.error("SMTP permanently dead: %s - %s", account.key, reason)

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
            if account.warmup_done:
                return 0.0
            now = time.monotonic()
            if account.last_used == 0.0:
                account.last_used = now
                return 0.0
            ready_at = account.last_used + self._warmup_delay
            if now >= ready_at:
                account.last_used = now
                return 0.0
            account.last_used = ready_at
            return ready_at - now

    def next_available_in(self) -> float:
        with self._lock:
            if any(a.is_available for a in self._accounts):
                return 0.0
            suspended = [
                a.suspended_until for a in self._accounts if not a.dead
            ]
            if not suspended:
                return -1.0
            now = time.monotonic()
            return max(0.0, min(suspended) - now)

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self._ignore_ssl_errors:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            return ctx

        if HAS_CERTIFI:
            return ssl.create_default_context(cafile=certifi.where())
        return ssl.create_default_context()

    def _make_socket(self, account: SMTPAccount):
        if account.proxy and HAS_SOCKS:
            p = account.proxy
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, p.host, p.port,
                        username=p.username or None, password=p.password or None)
            s.settimeout(self._timeout)
            s.connect((account.host, account.port))
            return s
        return None

    def connect(self, account: SMTPAccount) -> smtplib.SMTP:
        ctx = self._build_ssl_context()
        if not account.proxy and self._proxies:
            account.proxy = self.get_current_proxy()
        proxy_sock = self._make_socket(account)

        if account.port == 465:
            if proxy_sock:
                wrapped = ctx.wrap_socket(proxy_sock, server_hostname=account.host)
                server = smtplib.SMTP_SSL(context=ctx)
                server.sock = wrapped
                server._host = account.host
                server.file = server.sock.makefile("rb")
                server.getreply()
            else:
                server = smtplib.SMTP_SSL(
                    account.host, account.port,
                    timeout=self._timeout, context=ctx,
                )
            server.ehlo()
        else:
            if proxy_sock:
                server = smtplib.SMTP()
                server.sock = proxy_sock
                server._host = account.host
                server.file = server.sock.makefile("rb")
                server.getreply()
            else:
                server = smtplib.SMTP(account.host, account.port, timeout=self._timeout)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=ctx)
                server.ehlo()

        server.login(account.user, account.password)
        return server


class SMTPWorker:
    MAX_CONN_SENDS = 50
    MAX_CONN_AGE = 300.0

    def __init__(
        self,
        pool: SMTPPool,
        normal_delay: float = 0.3,
        provider_delay: float = 6.0,
    ):
        self._pool = pool
        self._normal_delay = normal_delay
        self._provider_delay = provider_delay
        self._local = threading.local()

    def _get_connection(self, account: SMTPAccount) -> Optional[smtplib.SMTP]:
        lc = self._local
        if not hasattr(lc, "server") or lc.server is None:
            return None
        if lc.account_key != account.key:
            self._drop_connection()
            return None
        if lc.send_count >= self.MAX_CONN_SENDS:
            self._drop_connection()
            return None
        if time.monotonic() - lc.conn_time > self.MAX_CONN_AGE:
            self._drop_connection()
            return None
        try:
            code, _ = lc.server.noop()
            if code != 250:
                self._drop_connection()
                return None
            return lc.server
        except Exception:
            self._drop_connection()
            return None

    def _store_connection(
        self, server: smtplib.SMTP, account: SMTPAccount
    ) -> None:
        lc = self._local
        lc.server = server
        lc.account_key = account.key
        lc.send_count = 0
        lc.conn_time = time.monotonic()

    def _drop_connection(self) -> None:
        lc = self._local
        if hasattr(lc, "server") and lc.server:
            try:
                lc.server.quit()
            except Exception:
                try:
                    lc.server.close()
                except Exception:
                    pass
            lc.server = None

    def _bump_send_count(self) -> None:
        lc = self._local
        if hasattr(lc, "send_count"):
            lc.send_count += 1

    def send(
        self,
        from_email: str,
        to_email: str,
        raw_message: str,
        account: Optional[SMTPAccount] = None,
    ) -> SendResult:
        if account is None:
            account = self._pool.acquire()
        if account is None:
            return SendResult(SendResult.TRANSIENT, "No SMTP servers available")

        warmup_wait = self._pool.get_warmup_delay(account)
        if warmup_wait > 0:
            time.sleep(warmup_wait)

        server = self._get_connection(account)

        try:
            if server is None:
                server = self._pool.connect(account)
                self._store_connection(server, account)

            server.sendmail(from_email, [to_email], raw_message)
            self._bump_send_count()
            self._pool.record_send(account)
            return SendResult(SendResult.SUCCESS)

        except smtplib.SMTPAuthenticationError as exc:
            self._drop_connection()
            self._pool.mark_dead(account, f"Auth failed: {exc}")
            logger.error("[%d] AUTH FAIL %s: %s", exc.smtp_code, account.key, exc)
            return SendResult(SendResult.TRANSIENT, str(exc), exc.smtp_code)

        except smtplib.SMTPRecipientsRefused as exc:
            codes = [c for c, _m in exc.recipients.values()]
            detail = f"Recipients refused: {exc.recipients}"
            logger.error("[RCPT] %s via %s: %s", to_email, account.key, detail)
            if all(c in FATAL_RECIPIENT_CODES for c in codes):
                return SendResult(SendResult.FATAL, detail, codes[0] if codes else 0)
            self._drop_connection()
            self._pool.suspend(account, detail)
            return SendResult(SendResult.TRANSIENT, detail, codes[0] if codes else 0)

        except smtplib.SMTPServerDisconnected as exc:
            self._drop_connection()
            detail = f"Disconnected: {exc}"
            logger.error("DISCONN %s: %s", account.key, exc)
            self._pool.suspend(account, detail)
            return SendResult(SendResult.TRANSIENT, detail)

        except smtplib.SMTPResponseException as exc:
            self._drop_connection()
            detail = f"[{exc.smtp_code}] {exc.smtp_error}"
            logger.error("[%d] %s via %s: %s", exc.smtp_code, to_email, account.key, exc.smtp_error)
            if exc.smtp_code in FATAL_RECIPIENT_CODES:
                return SendResult(SendResult.FATAL, detail, exc.smtp_code)
            self._pool.suspend(account, detail)
            return SendResult(SendResult.TRANSIENT, detail, exc.smtp_code)

        except ssl.SSLError as exc:
            self._drop_connection()
            detail = f"SSL: {exc}"
            logger.error("SSL %s: %s", account.key, exc)
            self._pool.suspend(account, detail)
            return SendResult(SendResult.TRANSIENT, detail)

        except OSError as exc:
            self._drop_connection()
            detail = f"Connection: {exc}"
            logger.error("CONN %s: %s", account.key, exc)
            self._pool.suspend(account, detail)
            return SendResult(SendResult.TRANSIENT, detail)

    def get_delay(self, to_email: str) -> float:
        domain = to_email.split("@")[1].lower() if "@" in to_email else ""
        if domain in STRICT_PROVIDERS:
            base = self._provider_delay
        else:
            base = self._normal_delay
        return max(0.05, random.gauss(base, base * 0.3))
