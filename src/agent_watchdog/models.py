"""Shared validation rules for persisted contracts."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]
Positive = Annotated[int, Field(gt=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class Versioned(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
