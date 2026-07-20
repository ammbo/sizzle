"""Upload artifacts to Cloudflare R2 (S3-compatible API)."""

from __future__ import annotations

import os
from pathlib import Path


def r2_configured() -> bool:
    return bool(
        os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
        and (os.environ.get("R2_ACCOUNT_ID") or os.environ.get("CLOUDFLARE_ACCOUNT_ID"))
        and os.environ.get("R2_BUCKET_NAME")
    )


def upload_file(local_path: Path | str, key: str, *, content_type: str = "application/octet-stream") -> str:
    """Upload a local file to R2. Returns the object key."""
    import boto3
    from botocore.config import Config

    account_id = os.environ.get("R2_ACCOUNT_ID") or os.environ["CLOUDFLARE_ACCOUNT_ID"]
    bucket = os.environ["R2_BUCKET_NAME"]
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )
    client.upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return key


def video_key(run_id: str) -> str:
    return f"runs/{run_id}/final_cut.mp4"
