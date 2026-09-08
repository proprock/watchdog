"""Provider-neutral observation envelope, schema version 1."""

from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from agent_watchdog.models import NonEmpty, Versioned

EventKind = Literal[
    "session.start",
    "session.end",
    "turn.start",
    "turn.end",
    "tool.start",
    "tool.finish",
    "compaction.start",
    "compaction.end",
    "agent.start",
    "agent.end",
    "waiting",
    "interrupt",
    "usage",
    "observation.gap",
    "unknown",
]
Availability = Literal["observed", "inferred", "unknown", "unavailable"]


class Envelope(Versioned):
    event_id: UUID = Field(default_factory=uuid4)
    provider: NonEmpty
    provider_version: NonEmpty | None = None
    surface: Literal["cli", "desktop", "unknown"] = "unknown"
    project_id: UUID
    checkout_id: UUID | None = None
    session_id: NonEmpty | None = None
    agent_id: NonEmpty | None = None
    parent_agent_id: NonEmpty | None = None
    turn_id: NonEmpty | None = None
    native_event_id: NonEmpty | None = None
    kind: EventKind
    occurred_at: AwareDatetime | None = None
    received_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["hook", "transcript", "manual"]
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    availability: dict[str, Availability] = Field(default_factory=dict)
    # Operational timestamps and bounded queue samples.  They are optional so
    # existing stored envelopes and disabled pipeline telemetry remain valid.
    delivery: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def provider_namespace(self) -> Self:
        if self.payload and set(self.payload) != {self.provider}:
            raise ValueError("Payload must use the event provider as its namespace")
        return self
