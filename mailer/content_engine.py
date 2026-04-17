import os
import re
import random
import string
import html as html_module
from typing import Optional


class ContentEngine:
    _SPINTAX_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")
    _RANDSTR_RE = re.compile(r"\[RANDSTR:(\d+):([a-zA-Z0-9\-]+):(\w+)\]")
    _SPINTAX_FILE_RE = re.compile(r"\{spintax_(\w+)\}")

    CHARSET_MAP = {
        "a-z": string.ascii_lowercase,
        "A-Z": string.ascii_uppercase,
        "0-9": string.digits,
        "a-z0-9": string.ascii_lowercase + string.digits,
        "A-Z0-9": string.ascii_uppercase + string.digits,
        "a-zA-Z": string.ascii_letters,
        "a-zA-Z0-9": string.ascii_letters + string.digits,
    }

    def __init__(self, html_dir: str, attachments_dir: str, spintax_dir: str):
        self._html_dir = html_dir
        self._attachments_dir = attachments_dir
        self._spintax_dir = spintax_dir
        self._html_files: list = []
        self._attachment_files: list = []
        self._spintax_cache: dict = {}
        self._load_files()

    def _load_files(self) -> None:
        if os.path.isdir(self._html_dir):
            self._html_files = [
                os.path.join(self._html_dir, f)
                for f in os.listdir(self._html_dir)
                if f.lower().endswith((".html", ".htm"))
            ]
        if os.path.isdir(self._attachments_dir):
            self._attachment_files = [
                os.path.join(self._attachments_dir, f)
                for f in os.listdir(self._attachments_dir)
                if os.path.isfile(os.path.join(self._attachments_dir, f))
            ]

    def get_random_html(self) -> Optional[str]:
        if not self._html_files:
            return None
        path = random.choice(self._html_files)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def get_random_attachment(self) -> Optional[tuple]:
        if not self._attachment_files:
            return None
        path = random.choice(self._attachment_files)
        filename = os.path.basename(path)
        with open(path, "rb") as fh:
            data = fh.read()
        return filename, data

    def process(self, template: str, email: str) -> str:
        text = self._resolve_spintax_files(template)
        text = self._resolve_spintax(text)
        text = self._resolve_placeholders(text, email)
        text = self._resolve_randstr(text)
        return text

    def _resolve_spintax_files(self, text: str) -> str:
        def replacer(match: re.Match) -> str:
            filename = match.group(1)
            lines = self._load_spintax_file(filename)
            if lines:
                return random.choice(lines)
            return match.group(0)

        return self._SPINTAX_FILE_RE.sub(replacer, text)

    def _load_spintax_file(self, name: str) -> list:
        if name in self._spintax_cache:
            return self._spintax_cache[name]
        path = os.path.join(self._spintax_dir, f"{name}.txt")
        if not os.path.isfile(path):
            self._spintax_cache[name] = []
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [line.strip() for line in fh if line.strip()]
        self._spintax_cache[name] = lines
        return lines

    def _resolve_spintax(self, text: str) -> str:
        max_depth = 10
        for _ in range(max_depth):
            new_text = self._SPINTAX_RE.sub(
                lambda m: random.choice(m.group(1).split("|")), text
            )
            if new_text == text:
                break
            text = new_text
        return text

    @staticmethod
    def _resolve_placeholders(text: str, email: str) -> str:
        email_user = email.split("@")[0] if "@" in email else email
        domain = email.split("@")[1] if "@" in email else ""
        text = text.replace("{email}", email)
        text = text.replace("{email_user}", email_user)
        text = text.replace("{domain}", domain)
        return text

    def _resolve_randstr(self, text: str) -> str:
        def replacer(match: re.Match) -> str:
            length = int(match.group(1))
            charset_key = match.group(2)
            case = match.group(3).lower()

            charset = self.CHARSET_MAP.get(charset_key, charset_key)
            if not charset:
                charset = string.ascii_lowercase

            result = "".join(random.choices(charset, k=length))
            if case == "lower":
                result = result.lower()
            elif case == "upper":
                result = result.upper()
            return result

        return self._RANDSTR_RE.sub(replacer, text)

    @staticmethod
    def html_to_plaintext(html_content: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</tr>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        text = html_module.unescape(text)
        lines = [line.strip() for line in text.splitlines()]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
