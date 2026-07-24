"""Provider-neutral contracts for bounded, auditable model context."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationContextMessage(BaseModel):
    """Bounded role-preserving projection of one persisted Conversation message."""

    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: str = Field(max_length=256_000)
    source_ref: str = Field(min_length=1, max_length=300)
    source_kind: Literal["conversation_turn"] = "conversation_turn"
    trust: Literal["untrusted_data"] = "untrusted_data"
    turn_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    content_truncated: bool = False
