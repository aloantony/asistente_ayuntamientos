import os
import uuid
from collections.abc import Callable, Generator

# Configure the environment BEFORE importing the app so module-level
# singletons (settings, storage service) pick up test values.
os.environ["DOCUMENT_STORAGE_ROOT"] = "/tmp/test-document-storage"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.rate_limit import change_password_rate_limiter, login_rate_limiter
from app.core.security import create_access_token
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, organization_users
from app.rbac.models import (
    Group,
    Permission,
    Role,
    group_roles,
    role_permissions,
    user_groups,
)
from app.rbac.permissions import ensure_initial_permissions
from app.users.crud import create_user
from app.users.models import User

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://app:app@127.0.0.1:5432/app_test",
)
TEST_DATABASE_NAME = TEST_DATABASE_URL.rsplit("/", 1)[1]


@pytest.fixture(scope="session")
def engine() -> Generator[Engine, None, None]:
    server_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/app"
    server_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server_engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": TEST_DATABASE_NAME},
        ).scalar()
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))
    server_engine.dispose()

    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def db(engine: Engine) -> Generator[Session, None, None]:
    """Session wrapped in an outer transaction that is always rolled back.

    Application code calls session.commit() freely; with
    join_transaction_mode="create_savepoint" those commits release savepoints
    while the outer transaction keeps every test isolated.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    ensure_initial_permissions(session)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def reset_rate_limiters() -> Generator[None, None, None]:
    login_rate_limiter.reset()
    change_password_rate_limiter.reset()
    yield


@pytest.fixture()
def client(db: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def unique_suffix() -> str:
    return uuid.uuid4().hex[:10]


def headers_for(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def make_organization(db: Session) -> Callable[..., Organization]:
    def _make_organization(name: str | None = None, **kwargs) -> Organization:
        organization = Organization(
            name=name or f"Org {unique_suffix()}",
            **kwargs,
        )
        db.add(organization)
        db.commit()
        return organization

    return _make_organization


@pytest.fixture()
def make_user(db: Session) -> Callable[..., User]:
    def _make_user(
        *,
        email: str | None = None,
        password: str = "password-123",
        full_name: str = "Test User",
        is_active: bool = True,
        is_superuser: bool = False,
    ) -> User:
        return create_user(
            db,
            email=email or f"user-{unique_suffix()}@example.com",
            password=password,
            full_name=full_name,
            is_active=is_active,
            is_superuser=is_superuser,
        )

    return _make_user


@pytest.fixture()
def superuser(make_user) -> User:
    return make_user(is_superuser=True, full_name="Super User")


@pytest.fixture()
def add_member(db: Session) -> Callable[[User, Organization], None]:
    def _add_member(user: User, organization: Organization) -> None:
        exists = db.execute(
            select(organization_users).where(
                organization_users.c.organization_id == organization.id,
                organization_users.c.user_id == user.id,
            )
        ).first()
        if exists is None:
            db.execute(
                insert(organization_users).values(
                    organization_id=organization.id,
                    user_id=user.id,
                )
            )
            db.commit()

    return _add_member


@pytest.fixture()
def grant_permissions(
    db: Session,
    add_member,
) -> Callable[[User, Organization, list[str]], Group]:
    """Grant permission codes to a user inside one organization.

    Creates a dedicated group in the organization, a role holding the
    permissions, links everything, and enrolls the user as organization
    member (permissions only apply to members).
    """

    def _grant(
        user: User,
        organization: Organization,
        permission_codes: list[str],
    ) -> Group:
        add_member(user, organization)

        group = Group(
            name=f"grp-{unique_suffix()}",
            organization_id=organization.id,
        )
        role = Role(name=f"role-{unique_suffix()}")
        db.add_all([group, role])
        db.flush()

        permissions = list(
            db.scalars(
                select(Permission).where(Permission.code.in_(permission_codes))
            )
        )
        missing = set(permission_codes) - {p.code for p in permissions}
        if missing:
            raise ValueError(f"Unknown permission codes: {sorted(missing)}")

        db.execute(
            insert(group_roles).values(group_id=group.id, role_id=role.id)
        )
        for permission in permissions:
            db.execute(
                insert(role_permissions).values(
                    role_id=role.id,
                    permission_id=permission.id,
                )
            )
        db.execute(
            insert(user_groups).values(user_id=user.id, group_id=group.id)
        )
        db.commit()
        return group

    return _grant
