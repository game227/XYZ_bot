"""Private S3/MinIO bucket access (TZ §9.2). OWNER: COMMERCE.

Objects are never public: reads go through the media router (proxy mode) or through
presigned URLs that live `s3_presign_ttl_seconds`. Multipart uploads back the
resumable upload page.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings

logger = logging.getLogger(__name__)

_session = aioboto3.Session()


@asynccontextmanager
async def s3_client() -> AsyncIterator[Any]:
    settings = get_settings()
    async with _session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    ) as client:
        yield client


def _bucket() -> str:
    return get_settings().s3_bucket


async def ensure_bucket() -> None:
    """Create the private bucket if it is missing (dev convenience; idempotent)."""
    async with s3_client() as s3:
        try:
            await s3.head_bucket(Bucket=_bucket())
        except ClientError:
            await s3.create_bucket(Bucket=_bucket())
            logger.info("created bucket %s", _bucket())


async def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    async with s3_client() as s3:
        await s3.put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=content_type)


async def get_bytes(key: str) -> bytes:
    async with s3_client() as s3:
        obj = await s3.get_object(Bucket=_bucket(), Key=key)
        async with obj["Body"] as body:
            return await body.read()


async def upload_file(key: str, path: Path, content_type: str = "application/octet-stream") -> None:
    async with s3_client() as s3:
        await s3.upload_file(str(path), _bucket(), key, ExtraArgs={"ContentType": content_type})


async def download_file(key: str, path: Path) -> None:
    async with s3_client() as s3:
        await s3.download_file(_bucket(), key, str(path))


async def head(key: str) -> dict[str, Any] | None:
    async with s3_client() as s3:
        try:
            return await s3.head_object(Bucket=_bucket(), Key=key)
        except ClientError:
            return None


async def presigned_get(key: str, ttl_seconds: int | None = None) -> str:
    ttl = ttl_seconds or get_settings().s3_presign_ttl_seconds
    async with s3_client() as s3:
        return await s3.generate_presigned_url(
            "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=ttl
        )


async def delete_prefix(prefix: str) -> int:
    deleted = 0
    async with s3_client() as s3:
        paginator = s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                await s3.delete_objects(Bucket=_bucket(), Delete={"Objects": keys, "Quiet": True})
                deleted += len(keys)
    return deleted


# --- multipart (resumable uploads) ---------------------------------------------------------


async def create_multipart(key: str, content_type: str) -> str:
    async with s3_client() as s3:
        resp = await s3.create_multipart_upload(Bucket=_bucket(), Key=key, ContentType=content_type)
        return resp["UploadId"]


async def upload_part(key: str, upload_id: str, part_number: int, data: bytes) -> str:
    async with s3_client() as s3:
        resp = await s3.upload_part(
            Bucket=_bucket(), Key=key, UploadId=upload_id, PartNumber=part_number, Body=data
        )
        return resp["ETag"]


async def list_parts(key: str, upload_id: str) -> list[dict[str, Any]]:
    """Uploaded parts as [{"PartNumber", "ETag", "Size"}], sorted by part number."""
    parts: list[dict[str, Any]] = []
    async with s3_client() as s3:
        marker = 0
        while True:
            resp = await s3.list_parts(
                Bucket=_bucket(), Key=key, UploadId=upload_id, PartNumberMarker=marker, MaxParts=1000
            )
            parts.extend(
                {"PartNumber": p["PartNumber"], "ETag": p["ETag"], "Size": p["Size"]}
                for p in resp.get("Parts", [])
            )
            if not resp.get("IsTruncated"):
                break
            marker = resp["NextPartNumberMarker"]
    return sorted(parts, key=lambda p: p["PartNumber"])


async def complete_multipart(key: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
    async with s3_client() as s3:
        await s3.complete_multipart_upload(
            Bucket=_bucket(),
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in parts]},
        )


async def abort_multipart(key: str, upload_id: str) -> None:
    async with s3_client() as s3:
        try:
            await s3.abort_multipart_upload(Bucket=_bucket(), Key=key, UploadId=upload_id)
        except ClientError:
            logger.warning("abort multipart failed for %s", key)
