"""ASGI and console entry point for the API process."""

import uvicorn

from nico_agent.api import create_app
from nico_agent.config import get_settings

app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "nico_agent.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment == "development",
        log_config=None,
    )


if __name__ == "__main__":
    run()
