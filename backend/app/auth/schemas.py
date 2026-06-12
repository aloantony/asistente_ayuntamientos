from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BootstrapAdminRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1, max_length=255)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class DetailResponse(BaseModel):
    detail: str
