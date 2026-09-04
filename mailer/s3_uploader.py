"""AWS S3 Uploader mit SigV4 — public-read PUT ohne boto3.

Recycled den SigV4-Signer aus ses_api. Für die Logo-CDN-Nutzung reicht
uns PUT /bucket/key mit acl=public-read. Bucket muss "Block Public
Access" deaktiviert haben und ideally eine Bucket-Policy für
public-read (oder das ACL-Header greift).
"""
from __future__ import annotations
import datetime
import hashlib
import hmac
import logging
from typing import Tuple, Optional

logger = logging.getLogger("mailer.s3")

ALGO = "AWS4-HMAC-SHA256"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signature_key(secret: str, date_stamp: str, region: str,
                    service: str = "s3") -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _sigv4_put_headers(host: str, path: str, region: str,
                        body_bytes: bytes, content_type: str,
                        iam_key: str, iam_secret: str,
                        extra_headers: Optional[dict] = None) -> dict:
    """Build SigV4-signed headers for a PUT to S3."""
    now = datetime.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body_bytes).hexdigest()

    # Basis-Header (canonical + signed)
    hdrs = {
        "content-type":       content_type,
        "host":               host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date":         amz_date,
    }
    if extra_headers:
        for k, v in extra_headers.items():
            hdrs[k.lower()] = v

    signed_names = sorted(hdrs.keys())
    canonical_headers = "".join(f"{n}:{hdrs[n]}\n" for n in signed_names)
    signed_headers = ";".join(signed_names)

    canonical_request = (
        f"PUT\n{path}\n\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = (
        f"{ALGO}\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = _signature_key(iam_secret, date_stamp, region)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    hdrs["authorization"] = (
        f"{ALGO} Credential={iam_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return hdrs


def s3_bucket_url(bucket: str, region: str, key: str) -> str:
    """Public S3 URL fürs Objekt. us-east-1 hat keine region im hostname."""
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


class S3UploadError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def s3_upload_object(iam_key: str, iam_secret: str, region: str,
                      bucket: str, key: str, body: bytes,
                      content_type: str = "image/png",
                      public: bool = True,
                      timeout: int = 30) -> str:
    """PUT bytes → S3. Returns public URL. Wirft S3UploadError bei Fehler.

    public=True setzt x-amz-acl:public-read. Bucket muss dafür
    „Block Public Access" ausgeschaltet haben."""
    import requests
    host = (f"{bucket}.s3.amazonaws.com" if region == "us-east-1"
             else f"{bucket}.s3.{region}.amazonaws.com")
    path = f"/{key.lstrip('/')}"
    extra = {}
    if public:
        extra["x-amz-acl"] = "public-read"
    hdrs = _sigv4_put_headers(host, path, region, body,
                               content_type, iam_key, iam_secret,
                               extra_headers=extra)
    # requests will Content-Type case-insensitive matchen; wir übergeben
    # das lower-case dict wie signiert.
    url = f"https://{host}{path}"
    try:
        r = requests.put(url, data=body, headers=hdrs, timeout=timeout)
    except Exception as e:
        raise S3UploadError(f"HTTP failed: {e}", 0)
    if r.status_code >= 400:
        raise S3UploadError(f"S3 PUT [{r.status_code}]: {r.text[:400]}",
                            r.status_code)
    return s3_bucket_url(bucket, region, key)


def s3_ping(iam_key: str, iam_secret: str, region: str, bucket: str) -> dict:
    """Auth-Test: 1-byte object hochladen und wieder löschen. Nutzen nur
    für UI-„Test"-Button. Für den echten Upload-Path kein Overhead."""
    import secrets
    key = f".mailer-ping-{secrets.token_hex(6)}"
    try:
        url = s3_upload_object(iam_key, iam_secret, region, bucket, key,
                                b".", content_type="text/plain",
                                public=False)
        return {"ok": True, "url": url}
    except S3UploadError as e:
        return {"ok": False, "error": str(e), "status": e.status}


def parse_buckets_field(text: str) -> list:
    """Parse „bucket-a:eu-central-1, bucket-b:us-east-1" → [(bucket, region), ...]"""
    out = []
    for chunk in (text or "").replace("\n", ",").split(","):
        c = chunk.strip()
        if not c:
            continue
        if ":" in c:
            b, r = c.split(":", 1)
            out.append((b.strip(), r.strip() or "us-east-1"))
        else:
            out.append((c, "us-east-1"))
    return out
