from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AdminGroupSummary(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class PermissionRead(BaseModel):
    id: int
    code: str
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminRoleSummary(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class AdminUserSummary(BaseModel):
    id: int
    email: EmailStr
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class AdminUserRead(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    is_active: bool
    is_superuser: bool
    groups: list[AdminGroupSummary]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1, max_length=255)
    is_active: bool = True
    is_superuser: bool = False


class AdminUserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None
    is_superuser: bool | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class AdminGroupRead(BaseModel):
    id: int
    name: str
    description: str | None
    users: list[AdminUserSummary]
    roles: list[AdminRoleSummary]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(str_strip_whitespace=True)


class AdminGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(str_strip_whitespace=True)


class GroupMembershipResponse(BaseModel):
    group_id: int
    user_id: int
    detail: str


class RoleRead(BaseModel):
    id: int
    name: str
    description: str | None
    permissions: list[PermissionRead]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(str_strip_whitespace=True)


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(str_strip_whitespace=True)


class RoleDeleteResponse(BaseModel):
    role_id: int
    detail: str


class RolePermissionResponse(BaseModel):
    role_id: int
    permission_id: int
    detail: str


class GroupRoleResponse(BaseModel):
    group_id: int
    role_id: int
    detail: str


class PermissionBootstrapResponse(BaseModel):
    created_codes: list[str]
    permissions: list[PermissionRead]
    detail: str


class AdminUserDeleteResponse(BaseModel):
    user_id: int
    detail: str


class AdminGroupDeleteResponse(BaseModel):
    group_id: int
    detail: str
