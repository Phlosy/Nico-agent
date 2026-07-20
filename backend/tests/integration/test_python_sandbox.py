from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from nico_agent.config import Settings
from nico_agent.sandbox import DockerEngineSandboxRunner, SandboxExecutionRequest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1" or not Path("/var/run/docker.sock").exists(),
    reason="set RUN_INTEGRATION=1 with a local Docker Engine",
)


@pytest.mark.asyncio
async def test_real_python_container_is_non_root_readonly_networkless_and_cleaned() -> None:
    settings = Settings(
        _env_file=None,
        sandbox_wall_time_seconds=5,
        sandbox_output_bytes=4096,
        sandbox_pids_limit=8,
    )
    runner = DockerEngineSandboxRunner(settings)
    try:
        execution_id = uuid4()
        response = await runner.run(
            execution_id,
            SandboxExecutionRequest(
                code="""
import os
import socket

root_write_denied = False
try:
    with open('/nico-host-write', 'w') as target:
        target.write('denied')
except OSError:
    root_write_denied = True

network_denied = False
try:
    socket.create_connection(('1.1.1.1', 53), timeout=0.5)
except OSError:
    network_denied = True

with open('/tmp/allowed.txt', 'w') as target:
    target.write('temporary')

result = {
    'uid': os.getuid(),
    'gid': os.getgid(),
    'root_write_denied': root_write_denied,
    'network_denied': network_denied,
    'environment': dict(os.environ),
    'temporary': open('/tmp/allowed.txt').read(),
}
""",
                wall_time_seconds=5,
                output_bytes=4096,
                pids_limit=8,
            ),
        )

        assert response.status == "succeeded"
        assert response.result == {
            "uid": 65534,
            "gid": 65534,
            "root_write_denied": True,
            "network_denied": True,
            "environment": {},
            "temporary": "temporary",
        }

        truncated = await runner.run(
            uuid4(),
            SandboxExecutionRequest(
                code="print('x' * 10000); result = 'done'",
                output_bytes=1024,
            ),
        )
        assert truncated.status == "succeeded"
        assert len(truncated.stdout.encode()) <= 1024
        assert truncated.stdout_truncated is True

        timed_out_id = uuid4()
        timed_out = await runner.run(
            timed_out_id,
            SandboxExecutionRequest(code="while True: pass", wall_time_seconds=1),
        )
        assert timed_out.status == "timed_out"

        containers = await runner.client.get(
            "http://docker/v1.43/containers/json",
            params={
                "all": "true",
                "filters": json.dumps({"label": [f"nico.sandbox.execution_id={timed_out_id}"]}),
            },
        )
        assert containers.status_code == 200
        assert containers.json() == []
    finally:
        await runner.close()
