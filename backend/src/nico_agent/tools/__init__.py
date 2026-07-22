"""Provider-neutral tool contracts and registry."""

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
from nico_agent.tools.gateway import ToolGateway, ToolGatewayRequest, ToolGatewayResult
from nico_agent.tools.policy import (
    ToolAuthorization,
    authorize_tool,
    build_tool_policy_snapshot,
    narrow_tool_policy_snapshot,
)
from nico_agent.tools.registry import ToolRegistry

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
