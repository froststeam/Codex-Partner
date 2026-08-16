"""Validated request contracts shared by Codex Partner API routes."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, SecretStr


GoalStatus = Literal["active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"]


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1)
    goal: str = ""
    workspace: str = ""
    yolo: bool = True
    max_retries: int = Field(default=3, ge=0, le=100)
    retry_forever: bool = False
    provider_id: Optional[str] = None
    model: str = ""
    context: str = ""
    codex_session_id: str = ""
    reasoning_effort: str = ""
    service_tier: str = ""
    personality: str = ""
    collaboration_mode: Literal["default", "plan"] = "default"
    permission_profile: str = ""
    ssh_host: str = ""


class QuickTaskCreate(BaseModel):
    name: str = Field(default="新 Codex 会话", min_length=1, max_length=160)
    workspace: str = ""
    ssh_host: str = ""


class TaskPatch(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    goal: Optional[str] = None
    workspace: Optional[str] = None
    yolo: Optional[bool] = None
    max_retries: Optional[int] = Field(default=None, ge=0, le=100)
    retry_forever: Optional[bool] = None
    provider_id: Optional[str] = None
    model: Optional[str] = None
    codex_session_id: Optional[str] = None
    goal_status: Optional[GoalStatus] = None
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None
    personality: Optional[Literal["", "none", "friendly", "pragmatic"]] = None
    collaboration_mode: Optional[Literal["default", "plan"]] = None
    permission_profile: Optional[str] = None
    ssh_host: Optional[str] = None


class ContextPatch(BaseModel):
    context: str


class GoalPatch(BaseModel):
    objective: Optional[str] = None
    status: Optional[GoalStatus] = None


class WorkspaceFileUpdate(BaseModel):
    content: str = ""


class SSHConnectIn(BaseModel):
    host: str = Field(min_length=1, max_length=240)
    username: str = Field(default="", max_length=120)
    port: int = Field(default=22, ge=1, le=65535)
    password: Optional[SecretStr] = None


class SSHLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    content: str = ""
    enabled: bool = True


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "codex"
    model: str = ""
    model_provider: str = Field(default="", max_length=120, pattern=r"^[A-Za-z0-9._-]*$")
    profile: str = ""
    api_key: Optional[SecretStr] = None
    clear_api_key: bool = False
    base_url: str = ""
    enabled: bool = True
    priority: int = 100


class ProviderVerifyIn(BaseModel):
    """Ephemeral credentials used to verify a new provider before saving it."""

    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(default="", max_length=160)
    api_key: Optional[SecretStr] = None


class OperationIn(BaseModel):
    operation: str = Field(min_length=1, max_length=40)
    args: list[str] = Field(default_factory=list, max_length=32)
    prompt: str = ""


class TaskMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    client_message_id: Optional[str] = None
    delivery: Literal["auto", "queue"] = "auto"


class TaskMessagePatch(BaseModel):
    message: str = Field(min_length=1, max_length=20000)


class SlashCommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=20000)
    client_message_id: Optional[str] = None
    confirmed: bool = False


class MemoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    content: str = Field(default="", max_length=2_000_000)


class MemoryResetIn(BaseModel):
    confirm: Literal[True]
