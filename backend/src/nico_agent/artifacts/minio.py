"""Small async wrapper around the official synchronous MinIO SDK."""

from __future__ import annotations

import asyncio
from io import BytesIO
from urllib.parse import urlparse

from minio import Minio
from minio.commonconfig import CopySource


class MinioArtifactStore:
    def __init__(
        self,
        url: str,
        *,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("MinIO URL must be an HTTP(S) origin without a path")
        self.bucket = bucket
        self.client = Minio(
            parsed.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
        )

    async def put_temp(self, object_key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            self.bucket,
            object_key,
            BytesIO(data),
            len(data),
            content_type=content_type,
        )

    async def promote(self, temp_key: str, final_key: str) -> None:
        await asyncio.to_thread(
            self.client.copy_object,
            self.bucket,
            final_key,
            CopySource(self.bucket, temp_key),
        )
        await self.remove(temp_key)

    async def remove(self, object_key: str) -> None:
        await asyncio.to_thread(self.client.remove_object, self.bucket, object_key)

    async def read(self, object_key: str) -> bytes:
        response = await asyncio.to_thread(self.client.get_object, self.bucket, object_key)
        try:
            return await asyncio.to_thread(response.read)
        finally:
            response.close()
            response.release_conn()

    async def list_keys(self, prefix: str) -> list[str]:
        def collect() -> list[str]:
            return [
                item.object_name for item in self.client.list_objects(self.bucket, prefix=prefix)
            ]

        return await asyncio.to_thread(collect)
