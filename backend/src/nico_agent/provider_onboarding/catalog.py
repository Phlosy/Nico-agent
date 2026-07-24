"""Product-owned Provider catalog baseline."""

from __future__ import annotations

import os

from nico_agent.provider_onboarding.contracts import (
    ProviderCatalog,
    ProviderLocation,
    ProviderOption,
    ProviderPreset,
)


def _location(
    key: str,
    label: str,
    base_url: str,
    *,
    default: bool = True,
) -> ProviderLocation:
    return ProviderLocation(key=key, label=label, base_url=base_url, default=default)


CATALOG = ProviderCatalog(
    schema_version=1,
    catalog_revision="2026-07-24",
    providers=(
        ProviderPreset(
            key="openai",
            display_name="OpenAI",
            protocol="openai_compatible",
            locations=(_location("global", "Global", "https://api.openai.com/v1"),),
            discovery="openai_models",
            recommended_models=("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"),
            capabilities={
                "streaming": True,
                "native_tool_calling": True,
                "json_object": True,
                "json_schema": True,
            },
            documentation_url="https://developers.openai.com/api/reference/resources/models/methods/list",
        ),
        ProviderPreset(
            key="anthropic",
            display_name="Anthropic",
            protocol="anthropic_messages",
            locations=(_location("global", "Global", "https://api.anthropic.com"),),
            discovery="anthropic_models",
            recommended_models=(
                "claude-sonnet-5",
                "claude-opus-4-8",
                "claude-haiku-4-5",
            ),
            capabilities={"streaming": True, "native_tool_calling": True},
            documentation_url="https://platform.claude.com/docs/en/api/models/list",
        ),
        ProviderPreset(
            key="google-gemini",
            display_name="Google Gemini",
            protocol="google_gemini",
            locations=(
                _location("global", "Global", "https://generativelanguage.googleapis.com/v1beta"),
            ),
            discovery="gemini_models",
            recommended_models=("gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-2.5-flash"),
            capabilities={"streaming": True, "native_tool_calling": True},
            documentation_url="https://ai.google.dev/api/models",
        ),
        ProviderPreset(
            key="openrouter",
            display_name="OpenRouter",
            protocol="openai_compatible",
            locations=(_location("global", "Global", "https://openrouter.ai/api/v1"),),
            discovery="openai_models",
            recommended_models=("openrouter/auto", "openrouter/free"),
            capabilities={"streaming": True, "native_tool_calling": True},
            documentation_url="https://openrouter.ai/docs/guides/overview/models",
        ),
        ProviderPreset(
            key="xai",
            display_name="xAI",
            protocol="openai_compatible",
            locations=(_location("global", "Global", "https://api.x.ai/v1"),),
            discovery="openai_models",
            recommended_models=("grok-4.3", "grok-4.3-latest"),
            capabilities={"streaming": True, "native_tool_calling": True},
            documentation_url="https://docs.x.ai/developers/rest-api-reference/inference/models",
        ),
        ProviderPreset(
            key="deepseek",
            display_name="DeepSeek",
            protocol="openai_compatible",
            locations=(_location("global", "Global", "https://api.deepseek.com"),),
            discovery="openai_models",
            recommended_models=("deepseek-v4-flash", "deepseek-v4-pro"),
            capabilities={
                "streaming": True,
                "native_tool_calling": True,
                "json_object": True,
            },
            documentation_url="https://api-docs.deepseek.com/api/list-models",
        ),
        ProviderPreset(
            key="alibaba-bailian",
            display_name="Alibaba Cloud Bailian / Qwen",
            protocol="openai_compatible",
            locations=(
                _location(
                    "international",
                    "International",
                    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                ),
                _location(
                    "china",
                    "China mainland",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    default=False,
                ),
            ),
            discovery="curated",
            recommended_models=("qwen3.7-plus", "qwen3.7-max", "qwen3-coder-plus"),
            capabilities={"streaming": True, "native_tool_calling": True},
            options=(ProviderOption(key="workspace_id", label="Workspace ID"),),
            documentation_url="https://help.aliyun.com/zh/model-studio/text-generation",
        ),
        ProviderPreset(
            key="moonshot-kimi",
            display_name="Moonshot / Kimi",
            protocol="openai_compatible",
            locations=(_location("china", "China", "https://api.moonshot.cn/v1"),),
            discovery="curated",
            recommended_models=("kimi-k3", "kimi-k2.5"),
            capabilities={"streaming": True, "native_tool_calling": True},
            documentation_url="https://platform.kimi.com/docs/api/chat",
        ),
        ProviderPreset(
            key="zhipu-glm",
            display_name="Zhipu GLM",
            protocol="openai_compatible",
            locations=(_location("china", "China", "https://open.bigmodel.cn/api/paas/v4"),),
            discovery="curated",
            recommended_models=("glm-5.2", "glm-5"),
            capabilities={"streaming": True, "native_tool_calling": True},
            documentation_url="https://docs.bigmodel.cn/cn/guide/develop/http/introduction",
        ),
        ProviderPreset(
            key="minimax",
            display_name="MiniMax",
            protocol="openai_compatible",
            locations=(
                _location("global", "Global", "https://api.minimax.io/v1"),
                _location(
                    "china",
                    "China mainland",
                    "https://api.minimaxi.com/v1",
                    default=False,
                ),
            ),
            discovery="openai_models",
            recommended_models=("MiniMax-M2.7", "MiniMax-M2.5"),
            capabilities={"streaming": True, "native_tool_calling": True},
            documentation_url="https://platform.minimax.io/docs/api-reference/models/openai/list-models",
        ),
    ),
)


def get_provider_catalog() -> ProviderCatalog:
    base_url = os.getenv("NICO_PROVIDER_E2E_BASE_URL")
    if (
        base_url
        and os.getenv("NICO_PROVIDER_E2E_ALLOW_HTTP") == "true"
        and os.getenv("NICO_ENVIRONMENT", "development") != "production"
    ):
        suffixes = {
            "openai": "/openai/v1",
            "anthropic": "/anthropic",
            "google-gemini": "/gemini/v1beta",
        }
        providers = tuple(
            provider.model_copy(
                update={
                    "locations": (
                        _location(
                            "e2e",
                            "Deterministic E2E",
                            f"{base_url.rstrip('/')}{suffixes[provider.key]}",
                        ),
                    )
                }
            )
            if provider.key in suffixes
            else provider
            for provider in CATALOG.providers
        )
        return CATALOG.model_copy(update={"providers": providers})
    return CATALOG
