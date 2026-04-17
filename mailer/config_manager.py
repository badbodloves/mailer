import configparser
import os
import sys


class ConfigManager:
    REQUIRED_SECTIONS = ("paths", "sending", "sender", "test", "database")

    def __init__(self, config_path: str = "config.ini"):
        self._parser = configparser.ConfigParser()
        if not os.path.isfile(config_path):
            print(f"[!] Config file not found: {config_path}")
            sys.exit(1)
        self._parser.read(config_path, encoding="utf-8")
        self._validate()

    def _validate(self) -> None:
        for section in self.REQUIRED_SECTIONS:
            if not self._parser.has_section(section):
                print(f"[!] Missing config section: [{section}]")
                sys.exit(1)

    def get(self, section: str, key: str, fallback: str = "") -> str:
        return self._parser.get(section, key, fallback=fallback).strip()

    def getint(self, section: str, key: str, fallback: int = 0) -> int:
        return self._parser.getint(section, key, fallback=fallback)

    def getfloat(self, section: str, key: str, fallback: float = 0.0) -> float:
        return self._parser.getfloat(section, key, fallback=fallback)

    @property
    def smtp_file(self) -> str:
        return self.get("paths", "smtp_file")

    @property
    def leads_file(self) -> str:
        return self.get("paths", "leads_file")

    @property
    def html_dir(self) -> str:
        return self.get("paths", "html_dir")

    @property
    def attachments_dir(self) -> str:
        return self.get("paths", "attachments_dir")

    @property
    def spintax_dir(self) -> str:
        return self.get("paths", "spintax_dir")

    @property
    def thread_count(self) -> int:
        return self.getint("sending", "threads", fallback=40)

    @property
    def normal_delay(self) -> float:
        return self.getfloat("sending", "normal_delay", fallback=0.3)

    @property
    def provider_delay(self) -> float:
        return self.getfloat("sending", "provider_delay", fallback=6.0)

    @property
    def warmup_delay(self) -> float:
        return self.getfloat("sending", "warmup_delay", fallback=30.0)

    @property
    def warmup_count(self) -> int:
        return self.getint("sending", "warmup_count", fallback=5)

    @property
    def smtp_timeout(self) -> int:
        return self.getint("sending", "smtp_timeout", fallback=30)

    @property
    def from_name(self) -> str:
        return self.get("sender", "from_name")

    @property
    def from_email(self) -> str:
        return self.get("sender", "from_email")

    @property
    def subject(self) -> str:
        return self.get("sender", "subject")

    @property
    def test_recipients(self) -> list:
        raw = self.get("test", "test_recipients")
        if not raw:
            return []
        return [r.strip() for r in raw.split(",") if r.strip()]

    @property
    def db_path(self) -> str:
        return self.get("database", "db_path", fallback="mailer.db")
