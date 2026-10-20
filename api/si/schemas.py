from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "developer", "viewer"] = "developer"


class CollectionCreate(Input):
    name: str = Field(min_length=1, max_length=200)


class Search(Input):
    query: str = Field(min_length=1, max_length=10000)
    limit: int = Field(default=5, ge=1, le=50)


class MemoryCreate(Input):
    session_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100000)
    role: Literal["user", "assistant"] = "user"
    long_term: bool = False


class AgentCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    system_prompt: str = "You are a helpful assistant."
    model: str = "mock"
    tools: list[str] = Field(default_factory=list)
    collection_ids: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=8, ge=1, le=32)


class AgentPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    system_prompt: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    collection_ids: list[str] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=32)


class RunCreate(Input):
    input: str = Field(min_length=1, max_length=100000)
    session_id: str | None = Field(default=None, max_length=200)
    background: bool = True


class Approval(Input):
    approved: bool


class PromptCreate(Input):
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
    template: str = Field(min_length=1, max_length=100000)


class Render(Input):
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict)


class EvalItem(Input):
    input: str
    expected: Any


class EvalCreate(Input):
    model: str = Field(default="mock", min_length=1, max_length=200)
    dataset: list[EvalItem] = Field(min_length=1, max_length=1000)
    scorer: Literal["exact_match", "contains", "regex", "json_schema", "llm_judge"]
    agent_id: str | None = None
    prompt_name: str | None = None
    prompt_version: int | None = Field(default=None, ge=1)
    threshold: float = Field(default=1, ge=0, le=1)
