"""Provider-neutral tool contracts and registry."""

from typing import TYPE_CHECKING, Any

from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolExecutor,
    ToolIsolation,
    ToolRetryPolicy,
    ToolRisk,
    ToolSecretRequirements,
    executor_required_secret_names,
)
from nico_agent.tools.policy import (
    ToolAuthorization,
    authorize_tool,
    build_tool_policy_snapshot,
    narrow_tool_policy_snapshot,
)
from nico_agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nico_agent.tools.gateway import ToolGateway, ToolGatewayRequest, ToolGatewayResult

__all__ = [
    "ToolDefinitionSpec",
    "ToolAuthorization",
    "ToolExecutionContext",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolGateway",
    "ToolGatewayRequest",
    "ToolGatewayResult",
    "ToolIsolation",
    "ToolRegistry",
    "ToolRetryPolicy",
    "ToolRisk",
    "ToolSecretRequirements",
    "authorize_tool",
    "build_tool_policy_snapshot",
    "narrow_tool_policy_snapshot",
    "executor_required_secret_names",
]


def __getattr__(name: str) -> Any:
    """Load the Gateway lazily so low-level Tool modules remain independently importable."""

    if name in {"ToolGateway", "ToolGatewayRequest", "ToolGatewayResult"}:
        from nico_agent.tools.gateway import (
            ToolGateway,
            ToolGatewayRequest,
            ToolGatewayResult,
        )

        return {
            "ToolGateway": ToolGateway,
            "ToolGatewayRequest": ToolGatewayRequest,
            "ToolGatewayResult": ToolGatewayResult,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
