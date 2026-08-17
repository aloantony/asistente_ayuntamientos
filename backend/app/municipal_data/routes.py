from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.municipal_data.access import (
    get_municipal_data_organization_for_read,
    get_municipal_data_organization_for_write,
    require_municipal_data_permission,
)
from app.municipal_data.models import (
    ClimateRecord,
    HouseholdStat,
    PadronAnnualRecord,
    UtilitySupply,
    WaterMeter,
    WaterMeterReading,
)
from app.municipal_data.schemas import (
    ClimateRecordCreate,
    ClimateRecordRead,
    HouseholdStatCreate,
    HouseholdStatRead,
    PadronRecordCreate,
    PadronRecordRead,
    UtilitySupplyCreate,
    UtilitySupplyRead,
    WaterMeterCreate,
    WaterMeterRead,
    WaterMeterReadingCreate,
    WaterMeterReadingRead,
    WaterMeterStatus,
)
from app.users.models import User

router = APIRouter(prefix="/municipal-data", tags=["municipal-data"])


def _read_series(
    db: Session,
    current_user: User,
    organization_id: int,
    response: Response,
    page: PageParams,
    query: Select,
):
    """Las tres series anuales se leen igual: permiso, orden y paginación."""
    get_municipal_data_organization_for_read(db, organization_id)
    require_municipal_data_permission(
        db,
        current_user,
        organization_id,
        "municipal_data.view",
    )
    return list(db.scalars(paginate(db, query, page, response)))


def _create_row(
    db: Session,
    current_user: User,
    organization_id: int,
    row,
    conflict_detail: str,
):
    get_municipal_data_organization_for_write(db, organization_id)
    require_municipal_data_permission(
        db,
        current_user,
        organization_id,
        "municipal_data.edit",
    )
    db.add(row)
    commit_or_conflict(db, conflict_detail)
    db.refresh(row)
    return row


@router.get("/padron", response_model=list[PadronRecordRead])
def list_padron_records(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[PadronAnnualRecord]:
    query = (
        select(PadronAnnualRecord)
        .where(PadronAnnualRecord.organization_id == organization_id)
        # Del año más reciente hacia atrás: la serie se lee empezando por hoy.
        .order_by(PadronAnnualRecord.reference_year.desc())
    )
    return _read_series(db, current_user, organization_id, response, page, query)


@router.post(
    "/padron",
    response_model=PadronRecordRead,
    status_code=status.HTTP_201_CREATED,
)
def create_padron_record(
    payload: PadronRecordCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PadronAnnualRecord:
    return _create_row(
        db,
        current_user,
        payload.organization_id,
        PadronAnnualRecord(**payload.model_dump()),
        "A padrón record already exists for that year",
    )


@router.get("/climate", response_model=list[ClimateRecordRead])
def list_climate_records(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    reference_year: int | None = None,
) -> list[ClimateRecord]:
    query = (
        select(ClimateRecord)
        .where(ClimateRecord.organization_id == organization_id)
        .order_by(
            ClimateRecord.reference_year.desc(),
            ClimateRecord.reference_month,
        )
    )
    if reference_year is not None:
        query = query.where(ClimateRecord.reference_year == reference_year)
    return _read_series(db, current_user, organization_id, response, page, query)


@router.post(
    "/climate",
    response_model=ClimateRecordRead,
    status_code=status.HTTP_201_CREATED,
)
def create_climate_record(
    payload: ClimateRecordCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ClimateRecord:
    return _create_row(
        db,
        current_user,
        payload.organization_id,
        ClimateRecord(**payload.model_dump()),
        "A climate record already exists for that period",
    )


@router.get("/households", response_model=list[HouseholdStatRead])
def list_household_stats(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[HouseholdStat]:
    query = (
        select(HouseholdStat)
        .where(HouseholdStat.organization_id == organization_id)
        .order_by(HouseholdStat.reference_year.desc())
    )
    return _read_series(db, current_user, organization_id, response, page, query)


@router.post(
    "/households",
    response_model=HouseholdStatRead,
    status_code=status.HTTP_201_CREATED,
)
def create_household_stat(
    payload: HouseholdStatCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> HouseholdStat:
    return _create_row(
        db,
        current_user,
        payload.organization_id,
        HouseholdStat(**payload.model_dump()),
        "A household record already exists for that year",
    )


@router.get("/water/supplies", response_model=list[UtilitySupplyRead])
def list_water_supplies(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[UtilitySupply]:
    query = (
        select(UtilitySupply)
        .where(UtilitySupply.organization_id == organization_id)
        .order_by(UtilitySupply.name, UtilitySupply.id)
    )
    return _read_series(db, current_user, organization_id, response, page, query)


@router.post(
    "/water/supplies",
    response_model=UtilitySupplyRead,
    status_code=status.HTTP_201_CREATED,
)
def create_water_supply(
    payload: UtilitySupplyCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UtilitySupply:
    return _create_row(
        db,
        current_user,
        payload.organization_id,
        UtilitySupply(**payload.model_dump()),
        "Water supply could not be saved",
    )


@router.get("/water/meters", response_model=list[WaterMeterRead])
def list_water_meters(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        WaterMeterStatus | None,
        Query(alias="status"),
    ] = None,
    supply_id: int | None = None,
) -> list[WaterMeter]:
    query = (
        select(WaterMeter)
        .where(WaterMeter.organization_id == organization_id)
        .order_by(WaterMeter.code, WaterMeter.id)
    )
    if status_filter is not None:
        query = query.where(WaterMeter.status == status_filter)
    if supply_id is not None:
        query = query.where(WaterMeter.supply_id == supply_id)
    return _read_series(db, current_user, organization_id, response, page, query)


@router.post(
    "/water/meters",
    response_model=WaterMeterRead,
    status_code=status.HTTP_201_CREATED,
)
def create_water_meter(
    payload: WaterMeterCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WaterMeter:
    # El contador se sitúa en el territorio, así que aquí sí hace falta que la
    # organización tenga municipio, como en el inventario.
    organization = get_municipal_data_organization_for_write(
        db,
        payload.organization_id,
    )
    if organization.municipality_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization must have a municipality to register water meters",
        )
    require_municipal_data_permission(
        db,
        current_user,
        payload.organization_id,
        "municipal_data.edit",
    )
    if payload.supply_id is not None:
        ensure_supply_belongs_to_organization(
            db,
            supply_id=payload.supply_id,
            organization_id=payload.organization_id,
        )

    meter = WaterMeter(
        **payload.model_dump(),
        municipality_id=organization.municipality_id,
    )
    db.add(meter)
    commit_or_conflict(db, "Water meter code already exists")
    db.refresh(meter)
    return meter


@router.get(
    "/water/meters/{meter_id}/readings",
    response_model=list[WaterMeterReadingRead],
)
def list_water_meter_readings(
    meter_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[WaterMeterReading]:
    meter = get_existing_meter(db, meter_id)
    get_municipal_data_organization_for_read(db, meter.organization_id)
    require_municipal_data_permission(
        db,
        current_user,
        meter.organization_id,
        "municipal_data.view",
    )
    query = (
        select(WaterMeterReading)
        .where(WaterMeterReading.meter_id == meter.id)
        .order_by(WaterMeterReading.read_on.desc(), WaterMeterReading.id.desc())
    )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/water/meters/{meter_id}/readings",
    response_model=WaterMeterReadingRead,
    status_code=status.HTTP_201_CREATED,
)
def create_water_meter_reading(
    meter_id: int,
    payload: WaterMeterReadingCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WaterMeterReading:
    meter = get_existing_meter(db, meter_id)
    get_municipal_data_organization_for_write(db, meter.organization_id)
    require_municipal_data_permission(
        db,
        current_user,
        meter.organization_id,
        "municipal_data.edit",
    )
    if meter.status == "removed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Removed water meters cannot take readings",
        )

    reading = WaterMeterReading(
        **payload.model_dump(),
        meter_id=meter.id,
        organization_id=meter.organization_id,
    )
    db.add(reading)
    commit_or_conflict(db, "A reading already exists for that date")
    db.refresh(reading)
    return reading


def get_existing_meter(db: Session, meter_id: int) -> WaterMeter:
    meter = db.get(WaterMeter, meter_id)
    if meter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Water meter not found",
        )
    return meter


def ensure_supply_belongs_to_organization(
    db: Session,
    *,
    supply_id: int,
    organization_id: int,
) -> UtilitySupply:
    supply = db.get(UtilitySupply, supply_id)
    if supply is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Water supply not found",
        )
    if supply.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Water supply does not belong to the organization",
        )
    return supply


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
