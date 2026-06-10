from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

DocumentStatus = Literal["active", "archived"]


class DocumentRead(BaseModel):
    id: int
    organization_id: int
    project_id: int
    original_filename: str
    stored_filename: str
    storage_backend: str
    storage_key: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    status: DocumentStatus
    uploaded_by_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentUpdate(BaseModel):
    status: DocumentStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)
