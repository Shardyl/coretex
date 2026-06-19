"""Cloudflare R2 delivery layer — the `coretex-media` bucket.

Model: **Drive = source, Cortex = producer, R2 = delivery** (see docs/COMPANY-STANDARD.md). The operator
uploads masters to a company's Drive `asset_folder`; Cortex reads them, produces deliverables, and saves the
delivery copy here under `<company-slug>/<type>/<status>/<name>`, served publicly at `media.coretex.uk`.

Uploads use the R2 S3 credentials in /etc/cortex (R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
R2_BUCKET). The public URL works once `media.coretex.uk` is bound to the bucket (a Cloudflare write).
"""
from __future__ import annotations

import mimetypes

from . import config

PUBLIC_BASE = "https://media.coretex.uk"
TYPES = {"logos", "signatures", "newsletters", "blog", "pages", "social", "video", "brand", "misc"}
STATUSES = {"published", "draft", "archived"}


def _client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=config.require("R2_ENDPOINT"),
        aws_access_key_id=config.require("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=config.require("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def key(company: str, type_: str, name: str, status: str = "published") -> str:
    if type_ not in TYPES:
        type_ = "misc"
    if status not in STATUSES:
        status = "published"
    return f"{company}/{type_}/{status}/{name}"


def url(company: str, type_: str, name: str, status: str = "published") -> str:
    """The public delivery URL for an asset (whether or not it's uploaded yet)."""
    return f"{PUBLIC_BASE}/{key(company, type_, name, status)}"


def put(company: str, type_: str, name: str, data: bytes, *, status: str = "published",
        content_type: str | None = None) -> str:
    """Upload bytes to R2 and return the public media.coretex.uk URL."""
    k = key(company, type_, name, status)
    ct = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    _client().put_object(Bucket=config.require("R2_BUCKET"), Key=k, Body=data, ContentType=ct,
                         CacheControl="public, max-age=86400")
    return url(company, type_, name, status)


def put_file(company: str, type_: str, path: str, *, name: str | None = None,
             status: str = "published") -> str:
    """Upload a local file to R2 and return its public URL."""
    with open(path, "rb") as f:
        return put(company, type_, name or path.rsplit("/", 1)[-1], f.read(), status=status)
