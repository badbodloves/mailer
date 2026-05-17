"""S3 redirect link generator — creates a public bucket and uploads N HTML
redirect files. Returns path-style URLs."""
import logging
import secrets
import string
from typing import Optional

logger = logging.getLogger("mailer.s3_redirect")


def _redirect_html(destination: str) -> str:
    return (
        '<!DOCTYPE html><html><head>'
        '<meta http-equiv="refresh" content="0;url=' + destination + '">'
        '<script>window.location.replace(' + repr(destination) + ');</script>'
        '</head><body></body></html>'
    )


def _random_suffix(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def _new_bucket_name(prefix: str, tag: str = "") -> str:
    p = (prefix or "lk").lower().strip().replace("_", "-")
    suffix = _random_suffix(8)
    if tag:
        t = ''.join(c for c in tag.lower() if c.isalnum() or c == '-')[:16]
        if t:
            return f"{p}-{t}-{suffix}"
    return f"{p}-{suffix}"


_SOCKS_PATCHED = False


def _normalize_proxy(value: str) -> str:
    """Accept SMTP-style or URL-style proxies and emit a valid URL.

    Examples
        socks5://1.2.3.4:1080:user:pass  -> socks5://user:pass@1.2.3.4:1080
        1.2.3.4:1080:user:pass            -> http://user:pass@1.2.3.4:1080
        1.2.3.4:1080                      -> http://1.2.3.4:1080
        http://user:pass@host:port        -> unchanged
    """
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        scheme, rest = value.split("://", 1)
    else:
        scheme, rest = "http", value
    if "@" in rest:
        return f"{scheme}://{rest}"
    parts = rest.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"{scheme}://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        return f"{scheme}://{rest}"
    return f"{scheme}://{rest}"


def _patch_botocore_socks():
    """Teach botocore to use urllib3's SOCKSProxyManager for socks://
    proxy URLs. PySocks must be installed (it ships with urllib3[socks])."""
    global _SOCKS_PATCHED
    if _SOCKS_PATCHED:
        return
    try:
        from botocore.httpsession import URLLib3Session, ProxyConfiguration
        from urllib3.contrib.socks import SOCKSProxyManager
    except Exception:
        return

    SOCKS_SCHEMES = ("socks4://", "socks4a://", "socks5://", "socks5h://")
    SOCKS_PREFIXES = ("socks4:", "socks4a:", "socks5:", "socks5h:")

    # 1. ProxyConfiguration._fix_proxy_url only whitelists http:/https: —
    # without this patch every socks5:// proxy gets "http://" jammed in
    # front, producing the classic "http://socks5://..." double-prefix.
    _orig_fix = ProxyConfiguration._fix_proxy_url

    def _patched_fix(self, proxy_url):
        if proxy_url.startswith(SOCKS_PREFIXES):
            return proxy_url
        return _orig_fix(self, proxy_url)

    ProxyConfiguration._fix_proxy_url = _patched_fix

    # 2. _get_proxy_manager needs to hand SOCKS URLs to SOCKSProxyManager
    # instead of the default urllib3 ProxyManager (which only speaks HTTP).
    _orig_get = URLLib3Session._get_proxy_manager

    def _patched_get(self, proxy_url):
        if proxy_url in self._proxy_managers:
            return self._proxy_managers[proxy_url]
        if proxy_url.startswith(SOCKS_SCHEMES):
            proxy_headers = self._proxy_config.proxy_headers_for(proxy_url)
            pool_kwargs = self._get_pool_manager_kwargs(proxy_headers=proxy_headers)
            allowed = {k: v for k, v in pool_kwargs.items()
                        if k in ("num_pools", "headers", "maxsize", "block",
                                  "timeout", "retries", "ssl_context", "ca_certs",
                                  "ca_cert_dir", "cert_file", "key_file")}
            pm = SOCKSProxyManager(proxy_url, **allowed)
            # NOTE: deliberately do NOT overwrite pool_classes_by_scheme like
            # the parent does — SOCKSProxyManager ships SOCKS-aware pool
            # classes that pass _socks_options to the connection; the default
            # HTTPSConnection rejects that kwarg.
            self._proxy_managers[proxy_url] = pm
            return pm
        return _orig_get(self, proxy_url)

    URLLib3Session._get_proxy_manager = _patched_get
    _SOCKS_PATCHED = True


def make_s3_client(access_key: str, secret_key: str, region: str,
                    proxy: str = ""):
    """Build a boto3 S3 client. `proxy` accepts both URL- and SMTP-style
    strings and supports http://, https://, socks4://, socks5:// schemes.
    SOCKS routing requires PySocks (ships with urllib3[socks])."""
    import boto3
    kwargs = dict(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    proxy = _normalize_proxy(proxy)
    if proxy:
        if proxy.startswith(("socks4://", "socks4a://", "socks5://", "socks5h://")):
            _patch_botocore_socks()
        from botocore.config import Config
        kwargs["config"] = Config(proxies={"http": proxy, "https": proxy})
    return boto3.client("s3", **kwargs)


def create_public_bucket(s3, bucket: str, region: str):
    """Create bucket and configure for public-read static hosting."""
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )

    s3.delete_public_access_block(Bucket=bucket)

    s3.put_bucket_ownership_controls(
        Bucket=bucket,
        OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]},
    )

    import json as _json
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/*",
        }],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=_json.dumps(policy))


def generate_links(
    destination: str,
    count: int,
    access_key: str,
    secret_key: str,
    region: str,
    bucket_prefix: str = "lk",
    tag: str = "",
    progress_cb: Optional[callable] = None,
) -> list:
    """Create a fresh public bucket and upload `count` redirect HTMLs.
    Returns list of path-style public URLs.
    progress_cb(done, total, ok, errors) called after each upload."""
    s3 = make_s3_client(access_key, secret_key, region)

    bucket = _new_bucket_name(bucket_prefix, tag)
    for attempt in range(3):
        try:
            create_public_bucket(s3, bucket, region)
            break
        except s3.exceptions.BucketAlreadyOwnedByYou:
            break
        except s3.exceptions.BucketAlreadyExists:
            bucket = _new_bucket_name(bucket_prefix, tag)
        except Exception as e:
            if attempt == 2:
                raise
            logger.warning("Bucket creation retry %d: %s", attempt + 1, e)
            bucket = _new_bucket_name(bucket_prefix, tag)

    body = _redirect_html(destination).encode("utf-8")

    urls = []
    ok = 0
    errors = 0
    for i in range(count):
        key = f"{_random_suffix(10)}.html"
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="text/html; charset=utf-8",
                CacheControl="no-cache",
            )
            urls.append(f"https://s3.{region}.amazonaws.com/{bucket}/{key}")
            ok += 1
        except Exception as e:
            errors += 1
            logger.warning("S3 upload failed (%d/%d): %s", i + 1, count, e)
        if progress_cb:
            try:
                progress_cb(i + 1, count, ok, errors)
            except Exception:
                pass

    return urls
