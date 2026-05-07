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


def make_s3_client(access_key: str, secret_key: str, region: str):
    import boto3
    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


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
