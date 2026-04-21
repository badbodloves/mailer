"""Bulk SMTP Client.

Handles connection reuse, SSL, and VERP envelope-from for SES and
other SMTP providers. No proxy support (SES doesn't need it).
"""
import smtplib
import ssl
import time
import threading
import logging
from typing import Optional

logger = logging.getLogger("bulk.smtp")


class SMTPClient:
    MAX_SENDS = 50
    MAX_AGE = 300.0

    def __init__(self, host: str, port: int, username: str, password: str,
                 timeout: int = 30):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._local = threading.local()

    def send(self, envelope_from: str, to_email: str, raw_message: str) -> tuple:
        """Send a raw message. Returns (success: bool, error: str, code: int)."""
        server = self._get_conn()
        try:
            if server is None:
                server = self._connect()
                self._store(server)

            server.sendmail(envelope_from, [to_email], raw_message)
            self._bump()
            return True, "", 0

        except smtplib.SMTPAuthenticationError as exc:
            self._drop()
            logger.error("AUTH FAIL %s: %s", self._host, exc)
            return False, str(exc), exc.smtp_code

        except smtplib.SMTPRecipientsRefused as exc:
            codes = [c for c, _ in exc.recipients.values()]
            code = codes[0] if codes else 0
            detail = str(exc.recipients)
            if all(c >= 500 for c in codes):
                return False, f"FATAL:{detail}", code
            self._drop()
            return False, f"TRANSIENT:{detail}", code

        except smtplib.SMTPResponseException as exc:
            self._drop()
            return False, str(exc.smtp_error), exc.smtp_code

        except (ssl.SSLError, OSError) as exc:
            self._drop()
            logger.error("CONN %s: %s", self._host, exc)
            return False, str(exc), 0

    def _connect(self) -> smtplib.SMTP:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")

        if self._port == 465:
            server = smtplib.SMTP_SSL(self._host, self._port,
                                       timeout=self._timeout, context=ctx)
            server.ehlo()
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls(context=ctx)
                server.ehlo()

        server.login(self._username, self._password)
        return server

    def _get_conn(self) -> Optional[smtplib.SMTP]:
        lc = self._local
        if not hasattr(lc, "server") or lc.server is None:
            return None
        if lc.sends >= self.MAX_SENDS:
            self._drop()
            return None
        if time.monotonic() - lc.conn_time > self.MAX_AGE:
            self._drop()
            return None
        try:
            code, _ = lc.server.noop()
            if code != 250:
                self._drop()
                return None
            return lc.server
        except Exception:
            self._drop()
            return None

    def _store(self, server):
        lc = self._local
        lc.server = server
        lc.sends = 0
        lc.conn_time = time.monotonic()

    def _bump(self):
        if hasattr(self._local, "sends"):
            self._local.sends += 1

    def _drop(self):
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
