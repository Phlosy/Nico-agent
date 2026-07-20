"""Controlled HTTP access to private, content-addressed Artifacts."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from nico_agent.artifacts.contracts import RuntimeArtifactIntent
from nico_agent.artifacts.minio import MinioArtifactStore
from nico_agent.artifacts.service import ArtifactService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain_api import get_tenant_context

router = APIRouter(prefix="/api/v1", tags=["artifacts"])


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    owner_run_id: UUID
    name: str
    content_type: str
    artifact_type: str
    status: str
    sha256: str | None
    size_bytes: int | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    available_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


def get_service(request: Request) -> ArtifactService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Artifact database is unavailable")
    settings = request.app.state.settings
    return ArtifactService(
        database,
        MinioArtifactStore(
            settings.minio_url,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
        ),
        max_bytes=settings.artifact_max_bytes,
    )


Service = Annotated[ArtifactService, Depends(get_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.post("/runs/{run_id}/artifacts", status_code=201)
async def upload_artifact(
    run_id: UUID,
    request: Request,
    service: Service,
    context: Context,
    name: Annotated[str, Query(min_length=1, max_length=300)],
    idempotency_key: Annotated[str, Query(min_length=1, max_length=200)],
    content_type: Annotated[str, Query(min_length=1, max_length=200)] = "application/octet-stream",
    share_with_parent: bool = True,
):
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > service.max_bytes:
            raise DomainError(
                "ARTIFACT_TOO_LARGE",
                "artifact exceeds the configured size limit",
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    return await service.store_runtime_artifact(
        context,
        run_id,
        RuntimeArtifactIntent(
            name=name,
            content_type=content_type,
            content_base64=base64.b64encode(body).decode(),
            share_with_parent=share_with_parent,
            idempotency_key=idempotency_key,
        ),
    )


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactRead])
async def list_artifacts(run_id: UUID, service: Service, context: Context):
    return await service.list_for_run(context, run_id)


@router.get("/runs/{run_id}/artifacts/{artifact_id}/content")
async def download_artifact(
    run_id: UUID,
    artifact_id: UUID,
    service: Service,
    context: Context,
) -> Response:
    artifact, data = await service.read_for_run(context, run_id, artifact_id)
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={
            "Content-Length": str(len(data)),
            "ETag": f'"sha256:{artifact.sha256}"',
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.name, safe='')}",
        },
    )
