import os
import io
import json
import time
import random
import hashlib
import secrets
import logging
import base64
import copy
from typing import List, Optional, Tuple

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

NUM_TEMPLATES = 25


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
        mode: str = "cloudinary",
    ):
        self._cloud_name = cloud_name
        self._api_key = api_key
        self._api_secret = api_secret
        self._logos_dir = logos_dir
        self._mode = mode.lower().strip()
        self._enabled = enabled
        self._urls: List[str] = []
        self._templates: List[Image.Image] = []
        self._fmt: str = "PNG"

        if self._mode == "cloudinary":
            self._enabled = enabled and bool(cloud_name and api_key and api_secret)
            self._load_cache()
        elif self._mode == "cid":
            self._enabled = enabled and HAS_PILLOW
        else:
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def urls(self) -> List[str]:
        return self._urls

    @property
    def pool_size(self) -> int:
        if self._mode == "cloudinary":
            return len(self._urls)
        return len(self._templates)

    def prepare(self, lead_count: int) -> None:
        if not self._enabled:
            return
        if self._mode == "cloudinary":
            self._prepare_cloudinary(lead_count)
        elif self._mode == "cid":
            self._prepare_cid_templates()

    def get_random_url(self) -> str:
        if not self._urls:
            return ""
        return random.choice(self._urls)

    def get_cid_logo(self) -> Optional[Tuple[bytes, str, str]]:
        if not self._templates:
            return None
        template = random.choice(self._templates)
        img = template.copy()
        pixels = img.load()
        w, h = img.size
        x = w - 1 - random.randint(0, min(4, w - 1))
        y = h - 1 - random.randint(0, min(4, h - 1))
        pixel = pixels[x, y]

        if img.mode == "P":
            idx = pixel if isinstance(pixel, int) else pixel[0]
            delta = random.choice([-1, 1])
            pixels[x, y] = max(0, min(255, idx + delta))
        elif img.mode == "RGBA" and isinstance(pixel, tuple) and len(pixel) == 4:
            p = list(pixel[:3])
            ch = random.randint(0, 2)
            p[ch] = min(255, max(0, p[ch] + random.choice([-2, -1, 1, 2])))
            pixels[x, y] = (p[0], p[1], p[2], pixel[3])
        else:
            p = list(pixel[:3])
            ch = random.randint(0, 2)
            p[ch] = min(255, max(0, p[ch] + random.choice([-2, -1, 1, 2])))
            pixels[x, y] = (p[0], p[1], p[2])

        buf = io.BytesIO()
        save_kw = {}
        if self._fmt == "PNG":
            from PIL.PngImagePlugin import PngInfo
            meta = PngInfo()
            meta.add_text("uid", secrets.token_hex(8))
            save_kw["pnginfo"] = meta
            save_kw["optimize"] = True
            save_kw["compress_level"] = 9
        else:
            save_kw["quality"] = 95
            save_kw["optimize"] = True
        img.save(buf, format=self._fmt, **save_kw)
        raw = buf.getvalue()

        if self._fmt == "JPEG":
            uid = secrets.token_hex(8).encode()
            marker = b"\xff\xfe" + (len(uid) + 2).to_bytes(2, "big") + uid
            raw = raw[:2] + marker + raw[2:]

        cid = f"logo{secrets.token_hex(6)}"
        mime = "image/png" if self._fmt == "PNG" else "image/jpeg"
        return raw, cid, mime

    def _find_logos(self) -> List[str]:
        if not os.path.isdir(self._logos_dir):
            return []
        exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
        return sorted(
            os.path.join(self._logos_dir, f)
            for f in os.listdir(self._logos_dir)
            if f.lower().endswith(exts) and os.path.isfile(os.path.join(self._logos_dir, f))
        )

    def _prepare_cid_templates(self) -> None:
        if self._templates:
            return
        if not HAS_PILLOW:
            logger.error("Pillow not installed")
            return

        logos = self._find_logos()
        if not logos:
            logger.error("No images in %s/", self._logos_dir)
            self._enabled = False
            return

        base_path = logos[0]
        self._fmt = "PNG" if base_path.lower().endswith(".png") else "JPEG"
        base_img = Image.open(base_path)
        if base_img.mode == "P":
            base_img = base_img.convert("RGBA")
        elif base_img.mode not in ("RGB", "RGBA"):
            base_img = base_img.convert("RGB")

        has_transparency = False
        if base_img.mode == "RGBA":
            alpha = base_img.getchannel("A")
            has_transparency = alpha.getextrema()[0] < 255
            if not has_transparency:
                base_img = base_img.convert("RGB")

        w, h = base_img.size
        trans_str = "RGBA" if has_transparency else "P (palette)"
        print(f"  CID logos: generating {NUM_TEMPLATES} templates from "
              f"{os.path.basename(base_path)} [{trans_str}] ...")

        for i in range(NUM_TEMPLATES):
            rng = random.Random(i)
            scale = rng.uniform(0.98, 1.02)
            nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
            variant = base_img.resize((nw, nh), Image.LANCZOS) if (nw, nh) != (w, h) else base_img.copy()
            sx, sy = rng.choice([-1, 0, 1]), rng.choice([-1, 0, 1])
            if sx or sy:
                from PIL import ImageChops
                variant = ImageChops.offset(variant, sx, sy)
            if self._fmt == "PNG" and not has_transparency:
                variant = variant.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
            self._templates.append(variant)


        print(f"  CID logos: {len(self._templates)} templates ready")

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

    def _prepare_cloudinary(self, lead_count: int) -> None:
        if not HAS_PILLOW or not HAS_REQUESTS:
            logger.error("Pillow or requests not installed")
            return
        target = min(lead_count, self.MAX_VARIANTS)
        if len(self._urls) >= target:
            return

        logos = self._find_logos()
        if not logos:
            logger.error("No images in %s/", self._logos_dir)
            return

        needed = target - len(self._urls)
        print(f"  Cloudinary: uploading {needed} variants ...")
        for i in range(needed):
            vid = len(self._urls) + i
            base_path = logos[vid % len(logos)]
            data = self._obfuscate_for_upload(base_path, vid)
            if data is None:
                continue
            url = self._upload_to_cloudinary(data, f"logo_v{vid}")
            if url:
                self._urls.append(url)
                self._save_cache()
            if (i + 1) % 25 == 0 or (i + 1) == needed:
                print(f"    [{i + 1}/{needed}]")
        print(f"  Cloudinary pool: {len(self._urls)} URLs")

    @staticmethod
    def _obfuscate_for_upload(image_path: str, variant_id: int) -> Optional[bytes]:
        try:
            img = Image.open(image_path)
            if img.mode == "P":
                img = img.convert("RGBA")
            elif img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            rng = random.Random(variant_id)
            w, h = img.size
            scale = rng.uniform(0.98, 1.02)
            nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
            if (nw, nh) != (w, h):
                img = img.resize((nw, nh), Image.LANCZOS)
            sx, sy = rng.choice([-1, 0, 1]), rng.choice([-1, 0, 1])
            if sx or sy:
                from PIL import ImageChops
                img = ImageChops.offset(img, sx, sy)
            pixels = img.load()
            w, h = img.size
            for _ in range(rng.randint(3, 6)):
                x = w - 1 - rng.randint(0, min(9, w - 1))
                y = h - 1 - rng.randint(0, min(9, h - 1))
                ch = rng.randint(0, 2)
                delta = rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5])
                pixel = pixels[x, y]
                p = list(pixel[:3])
                p[ch] = min(255, max(0, p[ch] + delta))
                if img.mode == "RGBA" and len(pixel) == 4:
                    pixels[x, y] = (p[0], p[1], p[2], pixel[3])
                else:
                    pixels[x, y] = (p[0], p[1], p[2])
            buf = io.BytesIO()
            fmt = "PNG" if image_path.lower().endswith(".png") else "JPEG"
            uid = hashlib.md5(str(variant_id).encode()).hexdigest()
            kw = {}
            if fmt == "PNG":
                from PIL.PngImagePlugin import PngInfo
                m = PngInfo(); m.add_text("uid", uid); kw["pnginfo"] = m
            else:
                kw["quality"] = 95
            img.save(buf, format=fmt, **kw)
            raw = buf.getvalue()
            if fmt == "JPEG":
                c = uid.encode("ascii")
                marker = b"\xff\xfe" + (len(c) + 2).to_bytes(2, "big") + c
                raw = raw[:2] + marker + raw[2:]
            return raw
        except Exception as exc:
            logger.error("Obfuscation failed: %s", exc)
            return None

    def _upload_to_cloudinary(self, image_data: bytes, public_id: str) -> Optional[str]:
        timestamp = str(int(time.time()))
        to_sign = f"public_id={public_id}&timestamp={timestamp}{self._api_secret}"
        signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
        b64 = base64.b64encode(image_data).decode("ascii")
        url = f"https://api.cloudinary.com/v1_1/{self._cloud_name}/image/upload"
        try:
            resp = _requests.post(url, data={
                "file": f"data:image/png;base64,{b64}",
                "public_id": public_id, "timestamp": timestamp,
                "api_key": self._api_key, "signature": signature,
            }, timeout=60)
            if resp.status_code == 200:
                return resp.json().get("secure_url", "")
            logger.error("Cloudinary %d: %s", resp.status_code, resp.text[:300])
            return None
        except Exception as exc:
            logger.error("Cloudinary error: %s", exc)
            return None
