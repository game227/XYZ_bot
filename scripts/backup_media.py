"""Media bucket backup / restore (TZ §19).

    python scripts/backup_media.py backups/media                     # mirror bucket → backups/media/<bucket>/
    python scripts/backup_media.py --restore backups/media/xyz-media [--bucket NAME]

Backup is incremental: objects already present locally with the same size are skipped
(originals and HLS renditions are immutable once written). Restore uploads every local file
missing from the bucket (or with a different size), creating the bucket if needed.
"""

import argparse
import asyncio
from pathlib import Path

from botocore.exceptions import ClientError

from app.services import storage


async def mirror(dest: Path, bucket: str) -> tuple[int, int, int]:
    root = dest / bucket
    copied = skipped = total_bytes = 0
    async with storage.s3_client() as s3:
        paginator = s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                key, size = obj["Key"], obj["Size"]
                target = (root / key).resolve()
                if root.resolve() not in target.parents:  # never write outside the backup dir
                    continue
                total_bytes += size
                if target.exists() and target.stat().st_size == size:
                    skipped += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                part = target.with_name(target.name + ".part")
                await s3.download_file(bucket, key, str(part))
                part.replace(target)
                copied += 1
    return copied, skipped, total_bytes


async def restore(src: Path, bucket: str) -> tuple[int, int]:
    uploaded = skipped = 0
    async with storage.s3_client() as s3:
        try:
            await s3.head_bucket(Bucket=bucket)
        except ClientError:
            await s3.create_bucket(Bucket=bucket)
        files = await asyncio.to_thread(
            lambda: sorted(p for p in src.rglob("*") if p.is_file() and not p.name.endswith(".part"))
        )
        for path in files:
            key = path.relative_to(src).as_posix()
            try:
                head = await s3.head_object(Bucket=bucket, Key=key)
                if head["ContentLength"] == path.stat().st_size:
                    skipped += 1
                    continue
            except ClientError:
                pass
            await s3.upload_file(str(path), bucket, key)
            uploaded += 1
    return uploaded, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="backups/media")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--bucket", default=None, help="default: S3_BUCKET from settings")
    args = parser.parse_args()
    bucket = args.bucket or storage._bucket()
    if args.restore:
        uploaded, skipped = asyncio.run(restore(Path(args.path), bucket))
        print(f"media restore → {bucket}: {uploaded} uploaded, {skipped} already present")
    else:
        copied, skipped, total = asyncio.run(mirror(Path(args.path), bucket))
        print(f"media mirror: {copied} copied, {skipped} unchanged, {total / 1024 / 1024:.1f} MiB in bucket")


if __name__ == "__main__":
    main()
