import os
import io
import json
import time
import random
import hashlib
import logging
import base64
from typing import List, Optional

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("mailer.images")


class ImageManager:
    MAX_VARIANTS = 500
    CACHE_FILE = "image_pool.json"

    def __init__(
        self,
        enabled: bool = False,
        cloud_name: str = "",
        api_key: str = "",
        api_secret: str = "",
        logos_dir: str = "logos",
    ):
        self._enabled = enabled and bool(cloud_name and api_key and api_secret)
        self._cloud_name = cloud_name
        self._api_key = api_key
        self._api_secret = api_secret
        self._logos_dir = logos_dir
        self._urls: List[str] = []
        self._load_cache()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def urls(self) -> List[str]:
        return self._urls

    @property
    def pool_size(self) -> int:
        return len(self._urls)

    def _load_cache(self) -> None:
        if not os.path.isfile(self.CACHE_FILE):
            return
        try:
            with open(self.CACHE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    self._urls = [u for u in data if isinstance(u, str) and u]
        except (json.JSONDecodeError, OSError):
            self._urls = []

    def _save_cache(self) -> None:
        try:
            with open(self.CACHE_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._urls, fh, indent=2)
        except OSError as exc:
            logger.error("Failed to save image cache: %s", exc)

    def prepare(self, lead_count: int) -> None:
        if not self._enabled:
            return
        if not HAS_PILLOW:
            logger.error("Pillow not installed — image obfuscation unavailable")
            return
        if not HAS_REQUESTS:
            logger.error("requests not installed — Cloudinary upload unavailable")
            return

        target = min(lead_count, self.MAX_VARIANTS)
        if len(self._urls) >= target:
            return

        base_images = self._find_logos()
        if not base_images:
            logger.error("No images found in %s/", self._logos_dir)
            return

        needed = target - len(self._urls)
        print(f"  Image variants: generating + uploading {needed} ...")

        for i in range(needed):
            variant_id = len(self._urls) + i
            base_path = base_images[variant_id % len(base_images)]
            variant_data = self._obfuscate(base_path, variant_id)
            if variant_data is None:
                continue

            url = self._upload_to_cloudinary(variant_data, f"logo_v{variant_id}")
            if url:
                self._urls.append(url)
                self._save_cache()

            if (i + 1) % 25 == 0 or (i + 1) == needed:
                print(f"    [{i + 1}/{needed}] uploaded")

        print(f"  Image pool ready: {len(self._urls)} URLs")

    def get_random_url(self) -> str:
        if not self._urls:
            return ""
        return random.choice(self._urls)

    def _find_logos(self) -> List[str]:
        if not os.path.isdir(self._logos_dir):
            return []
        exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
        return sorted(
            os.path.join(self._logos_dir, f)
            for f in os.listdir(self._logos_dir)
            if f.lower().endswith(exts) and os.path.isfile(os.path.join(self._logos_dir, f))
        )

    @staticmethod
    def _obfuscate(image_path: str, variant_id: int) -> Optional[bytes]:
        try:
            img = Image.open(image_path)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

            pixels = img.load()
            w, h = img.size
            is_rgba = img.mode == "RGBA"
            rng = random.Random(variant_id)

            n_mods = rng.randint(3, 6)
            for _ in range(n_mods):
                x = w - 1 - rng.randint(0, min(9, w - 1))
                y = h - 1 - rng.randint(0, min(9, h - 1))
                channel = rng.randint(0, 2)
                delta = rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5])

                pixel = pixels[x, y]
                p = list(pixel[:3])
                p[channel] = min(255, max(0, p[channel] + delta))

                if is_rgba and len(pixel) == 4:
                    pixels[x, y] = (p[0], p[1], p[2], pixel[3])
                else:
                    pixels[x, y] = (p[0], p[1], p[2])

            buf = io.BytesIO()
            fmt = "PNG" if image_path.lower().endswith(".png") else "JPEG"
            save_kwargs = {}
            uid = hashlib.md5(str(variant_id).encode()).hexdigest()
            if fmt == "JPEG":
                save_kwargs["quality"] = 95
            if fmt == "PNG":
                from PIL.PngImagePlugin import PngInfo
                meta = PngInfo()
                meta.add_text("uid", uid)
                save_kwargs["pnginfo"] = meta
            img.save(buf, format=fmt, **save_kwargs)
            raw = buf.getvalue()
            if fmt == "JPEG":
                comment = uid.encode("ascii")
                marker = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment
                raw = raw[:2] + marker + raw[2:]
            return raw
        except Exception as exc:
            logger.error("Obfuscation failed for %s: %s", image_path, exc)
            return None

    def _upload_to_cloudinary(self, image_data: bytes, public_id: str) -> Optional[str]:
        timestamp = str(int(time.time()))
        to_sign = f"public_id={public_id}&timestamp={timestamp}{self._api_secret}"
        signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()

        b64 = base64.b64encode(image_data).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        url = f"https://api.cloudinary.com/v1_1/{self._cloud_name}/image/upload"
        try:
            resp = _requests.post(
                url,
                data={
                    "file": data_uri,
                    "public_id": public_id,
                    "timestamp": timestamp,
                    "api_key": self._api_key,
                    "signature": signature,
                },
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json().get("secure_url", "")
            logger.error("Cloudinary %d: %s", resp.status_code, resp.text[:300])
            return None
        except Exception as exc:
            logger.error("Cloudinary upload error: %s", exc)
            return None
