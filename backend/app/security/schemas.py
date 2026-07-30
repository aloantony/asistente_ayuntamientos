from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SecurityEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    outcome: str
    user_id: int | None
    actor_label: str | None
    organization_id: int | None
    client_ip: str | None
    user_agent: str | None
    target_type: str | None
    target_id: int | None
    detail: str | None
    created_at: datetime
