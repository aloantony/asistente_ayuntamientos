from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.administration.access import (
    get_administration_organization_for_read,
    get_administration_organization_for_write,
    require_administration_permission,
)
from app.administration.models import (
    MunicipalContract,
    MunicipalGrant,
    MunicipalLicence,
    MunicipalProcedure,
    OfficeHour,
    TransparencyItem,
)
from app.administration.schemas import (
    ContractCreate,
    ContractRead,
    ContractStatus,
    GrantCreate,
    GrantRead,
    GrantStatus,
    LicenceCreate,
    LicenceRead,
    LicenceStatus,
    OfficeHourCreate,
    OfficeHourRead,
    ProcedureCreate,
    ProcedureRead,
    ProcedureStatus,
    TransparencyArea,
    TransparencyItemCreate,
    TransparencyItemRead,
)
from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.users.models import User

router = APIRouter(prefix="/administration", tags=["administration"])

# Orden natural de la semana: alfabéticamente "friday" iría antes que "monday",
# que no es como nadie lee un horario.
WEEKDAY_ORDER = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}


def _read(
    db: Session,
    current_user: User,
    organization_id: int,
    response: Response,
    page: PageParams,
    query: Select,
):
    get_administration_organization_for_read(db, organization_id)
    require_administration_permission(
        db,
        current_user,
        organization_id,
        "administration.view",
    )
    return list(db.scalars(paginate(db, query, page, response)))


def _create(db: Session, current_user: User, organization_id: int, row, detail: str):
    get_administration_organization_for_write(db, organization_id)
    require_administration_permission(
        db,
        current_user,
        organization_id,
        "administration.edit",
    )
    db.add(row)
    commit_or_conflict(db, detail)
    db.refresh(row)
    return row


@router.get("/office-hours", response_model=list[OfficeHourRead])
def list_office_hours(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[OfficeHour]:
    query = (
        select(OfficeHour)
        .where(OfficeHour.organization_id == organization_id)
        .order_by(OfficeHour.office_name, OfficeHour.opens_at, OfficeHour.id)
    )
    rows = _read(db, current_user, organization_id, response, page, query)
    return sorted(
        rows,
        key=lambda row: (
            row.office_name,
            WEEKDAY_ORDER.get(row.weekday, 8),
            row.opens_at,
        ),
    )


@router.post(
    "/office-hours",
    response_model=OfficeHourRead,
    status_code=status.HTTP_201_CREATED,
)
def create_office_hour(
    payload: OfficeHourCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OfficeHour:
    return _create(
        db,
        current_user,
        payload.organization_id,
        OfficeHour(**payload.model_dump()),
        "Office hours could not be saved",
    )


@router.get("/procedures", response_model=list[ProcedureRead])
def list_procedures(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        ProcedureStatus | None,
        Query(alias="status"),
    ] = None,
    include_archived: bool = False,
) -> list[MunicipalProcedure]:
    query = (
        select(MunicipalProcedure)
        .where(MunicipalProcedure.organization_id == organization_id)
        .order_by(MunicipalProcedure.name, MunicipalProcedure.id)
    )
    if status_filter is not None:
        query = query.where(MunicipalProcedure.status == status_filter)
    elif not include_archived:
        query = query.where(MunicipalProcedure.status != "archived")
    return _read(db, current_user, organization_id, response, page, query)


@router.post(
    "/procedures",
    response_model=ProcedureRead,
    status_code=status.HTTP_201_CREATED,
)
def create_procedure(
    payload: ProcedureCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalProcedure:
    return _create(
        db,
        current_user,
        payload.organization_id,
        MunicipalProcedure(**payload.model_dump()),
        "A procedure with that slug already exists",
    )


@router.get("/licences", response_model=list[LicenceRead])
def list_licences(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        LicenceStatus | None,
        Query(alias="status"),
    ] = None,
) -> list[MunicipalLicence]:
    query = (
        select(MunicipalLicence)
        .where(MunicipalLicence.organization_id == organization_id)
        # De la más reciente hacia atrás: lo que se consulta es lo que acaba de
        # entrar, no lo que se resolvió hace años.
        .order_by(MunicipalLicence.requested_on.desc(), MunicipalLicence.id.desc())
    )
    if status_filter is not None:
        query = query.where(MunicipalLicence.status == status_filter)
    return _read(db, current_user, organization_id, response, page, query)


@router.post(
    "/licences",
    response_model=LicenceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_licence(
    payload: LicenceCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalLicence:
    return _create(
        db,
        current_user,
        payload.organization_id,
        MunicipalLicence(**payload.model_dump()),
        "A licence with that reference already exists",
    )


@router.get("/contracts", response_model=list[ContractRead])
def list_contracts(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        ContractStatus | None,
        Query(alias="status"),
    ] = None,
) -> list[MunicipalContract]:
    query = (
        select(MunicipalContract)
        .where(MunicipalContract.organization_id == organization_id)
        .order_by(
            MunicipalContract.published_on.desc().nullslast(),
            MunicipalContract.id.desc(),
        )
    )
    if status_filter is not None:
        query = query.where(MunicipalContract.status == status_filter)
    return _read(db, current_user, organization_id, response, page, query)


@router.post(
    "/contracts",
    response_model=ContractRead,
    status_code=status.HTTP_201_CREATED,
)
def create_contract(
    payload: ContractCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalContract:
    return _create(
        db,
        current_user,
        payload.organization_id,
        MunicipalContract(**payload.model_dump()),
        "A contract with that reference already exists",
    )


@router.get("/grants", response_model=list[GrantRead])
def list_grants(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        GrantStatus | None,
        Query(alias="status"),
    ] = None,
) -> list[MunicipalGrant]:
    query = (
        select(MunicipalGrant)
        .where(MunicipalGrant.organization_id == organization_id)
        .order_by(
            MunicipalGrant.application_deadline.asc().nullslast(),
            MunicipalGrant.id,
        )
    )
    if status_filter is not None:
        query = query.where(MunicipalGrant.status == status_filter)
    return _read(db, current_user, organization_id, response, page, query)


@router.post(
    "/grants",
    response_model=GrantRead,
    status_code=status.HTTP_201_CREATED,
)
def create_grant(
    payload: GrantCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalGrant:
    return _create(
        db,
        current_user,
        payload.organization_id,
        MunicipalGrant(**payload.model_dump()),
        "Grant could not be saved",
    )


@router.get("/transparency", response_model=list[TransparencyItemRead])
def list_transparency_items(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    area: TransparencyArea | None = None,
) -> list[TransparencyItem]:
    query = (
        select(TransparencyItem)
        .where(TransparencyItem.organization_id == organization_id)
        .order_by(TransparencyItem.area, TransparencyItem.title, TransparencyItem.id)
    )
    if area is not None:
        query = query.where(TransparencyItem.area == area)
    return _read(db, current_user, organization_id, response, page, query)


@router.post(
    "/transparency",
    response_model=TransparencyItemRead,
    status_code=status.HTTP_201_CREATED,
)
def create_transparency_item(
    payload: TransparencyItemCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TransparencyItem:
    return _create(
        db,
        current_user,
        payload.organization_id,
        TransparencyItem(**payload.model_dump()),
        "Transparency item could not be saved",
    )


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
