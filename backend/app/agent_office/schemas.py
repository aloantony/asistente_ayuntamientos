from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AgentOfficeDepartment = Literal[
    "front_desk",
    "requirements",
    "ordinances",
    "documents",
    "projects",
    "map",
    "daily_briefing",
]
AgentOfficeTaskStatus = Literal[
    "pending_approval",
    "approved",
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]
AgentOfficePriority = Literal["low", "medium", "high", "urgent"]
AgentOfficeApprovalPolicy = Literal[
    "never",
    "before_execution",
    "after_draft",
    "always",
]
AgentOfficeRoutineKind = Literal["daily_briefing"]
AgentOfficeRoutineStatus = Literal["active", "paused", "archived"]
AgentOfficeRoutineCadence = Literal["daily", "manual"]
AgentOfficeTargetChannel = Literal["web", "telegram", "none"]
AgentOfficeApprovalDecision = Literal["approve", "cancel"]


class AgentOfficeAgentRead(BaseModel):
    key: str
    name: str
    department: AgentOfficeDepartment
    description: str
    assistant_agent_key: str
    tool_names: list[str]
    mutating_actions: list[str]
    requires_approval_by_default: bool


class AgentOfficeStatusRead(BaseModel):
    enabled: bool = True
    agents: list[AgentOfficeAgentRead]
    default_approval_policy: AgentOfficeApprovalPolicy = "before_execution"
    scheduler: dict = Field(default_factory=dict)


class AgentOfficeTaskEventRead(BaseModel):
    id: int
    task_id: int
    event_type: str
    message: str
    payload: dict = Field(default_factory=dict)
    created_by_id: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentOfficeTaskCreate(BaseModel):
    organization_id: int
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=6000)
    department: AgentOfficeDepartment | None = None
    requested_action: str | None = Field(default=None, min_length=1, max_length=120)
    priority: AgentOfficePriority = "medium"
    approval_policy: AgentOfficeApprovalPolicy | None = None
    requires_human_approval: bool | None = None
    input: dict = Field(default_factory=dict)
    due_at: datetime | None = None
    scheduled_for: datetime | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class AgentOfficeTaskRead(BaseModel):
    id: int
    organization_id: int
    title: str
    description: str
    department: AgentOfficeDepartment
    requested_action: str
    priority: AgentOfficePriority
    status: AgentOfficeTaskStatus
    approval_policy: AgentOfficeApprovalPolicy
    requires_human_approval: bool
    assigned_agent_key: str | None
    routing_reason: str | None
    input: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    error_message: str | None
    due_at: datetime | None
    scheduled_for: datetime | None
    source_conversation_id: int | None
    source_message_id: int | None
    requested_by_id: int | None
    approved_by_id: int | None
    approved_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentOfficeTaskDetail(AgentOfficeTaskRead):
    events: list[AgentOfficeTaskEventRead] = []


class AgentOfficeTaskApproval(BaseModel):
    decision: AgentOfficeApprovalDecision
    notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AgentOfficeTaskEnqueueRead(BaseModel):
    task_id: int
    status: AgentOfficeTaskStatus
    queue_job_id: str | None = None


class AgentOfficeRoutineCreate(BaseModel):
    organization_id: int
    name: str = Field(min_length=1, max_length=255)
    kind: AgentOfficeRoutineKind = "daily_briefing"
    status: AgentOfficeRoutineStatus = "active"
    cadence: AgentOfficeRoutineCadence = "daily"
    schedule_time: str = Field(default="09:15", pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(default="Europe/Madrid", min_length=1, max_length=80)
    target_channel: AgentOfficeTargetChannel = "web"
    config: dict = Field(default_factory=dict)
    next_run_at: datetime | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class AgentOfficeRoutineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: AgentOfficeRoutineStatus | None = None
    cadence: AgentOfficeRoutineCadence | None = None
    schedule_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    target_channel: AgentOfficeTargetChannel | None = None
    config: dict | None = None
    next_run_at: datetime | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class AgentOfficeRoutineRead(BaseModel):
    id: int
    organization_id: int
    name: str
    kind: AgentOfficeRoutineKind
    status: AgentOfficeRoutineStatus
    cadence: AgentOfficeRoutineCadence
    schedule_time: str
    timezone: str
    target_channel: AgentOfficeTargetChannel
    config: dict = Field(default_factory=dict)
    created_by_id: int | None
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
