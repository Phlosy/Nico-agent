"""Private authenticated HTTP surface for the independent Sandbox Runner."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status

from nico_agent.config import Settings, get_settings
from nico_agent.sandbox.contracts import SandboxExecutionRequest, SandboxExecutionResponse
from nico_agent.sandbox.docker_runner import DockerEngineSandboxRunner, SandboxRunnerFailure


def create_sandbox_runner_app(
    settings: Settings | None = None,
    runner: DockerEngineSandboxRunner | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_runner = runner or DockerEngineSandboxRunner(resolved_settings)
    semaphore = asyncio.Semaphore(resolved_settings.sandbox_max_concurrency)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await resolved_runner.close()

    app = FastAPI(
        title="Nico Sandbox Runner",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {resolved_settings.sandbox_runner_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        if not await resolved_runner.health():
            raise HTTPException(status_code=503, detail="Docker Engine unavailable")
        return {"status": "ok"}

    @app.post(
        "/v1/executions/{execution_id}",
        response_model=SandboxExecutionResponse,
        dependencies=[Depends(authenticate)],
    )
    async def execute(
        execution_id: UUID,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResponse:
        async with semaphore:
            try:
                return await resolved_runner.run(execution_id, request)
            except SandboxRunnerFailure as exc:
                status_code = (
                    status.HTTP_409_CONFLICT
                    if exc.code == "SANDBOX_EXECUTION_CONFLICT"
                    else status.HTTP_503_SERVICE_UNAVAILABLE
                )
                raise HTTPException(status_code=status_code, detail=exc.code) from exc

    @app.delete(
        "/v1/executions/{execution_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(authenticate)],
    )
    async def cancel(execution_id: UUID) -> Response:
        await resolved_runner.cancel(execution_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def run() -> None:
    uvicorn.run(
        "nico_agent.sandbox.api:create_sandbox_runner_app",
        host="0.0.0.0",
        port=8090,
        factory=True,
        reload=False,
        log_config=None,
    )


if __name__ == "__main__":
    run()
