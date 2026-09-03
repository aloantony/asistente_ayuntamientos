import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.rate_limit import require_rate_limit_slot, upload_rate_limiter
from app.db.session import get_db
from app.documents.storage import (
    DEFAULT_EXTENSIONS_BY_CONTENT_TYPE,
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidStorageKeyError,
    LocalStorageService,
    UnsupportedDocumentContentTypeError,
)
from app.town_hall.access import (
    require_town_hall_edit,
    require_town_hall_view,
    resolve_organization,
)
from app.town_hall import weather
from app.town_hall.models import MunicipalBlock, MunicipalProfile
from app.town_hall.seed import (
    EPIGRAPH_MODULES,
    ensure_initial_town_hall_structure,
    read_seed_key,
)
from app.town_hall.schemas import (
    BLOCK_PARENT_TYPES,
    SECTION_LAYOUTS,
    MunicipalBlockCreate,
    MunicipalBlockRead,
    MunicipalBlockReorder,
    MunicipalBlockUpdate,
    MAX_ITEM_ATTACHMENTS,
    MunicipalAttachmentRead,
    MunicipalContentField,
    MunicipalContentItemRead,
    MunicipalContentRead,
    MunicipalNavEpigraphRead,
    MunicipalNavItemRead,
    MunicipalNavSectionRead,
    MunicipalProfileRead,
    MunicipalProfileUpdate,
    MunicipalSeriesPoint,
    MunicipalWeatherRead,
    TownHallRead,
    TownHallStructureSeedResult,
)
from app.users.models import User

router = APIRouter(prefix="/town-hall", tags=["town-hall"])
storage_service = LocalStorageService()


def get_profile(db: Session, organization_id: int) -> MunicipalProfile | None:
    return db.scalar(
        select(MunicipalProfile).where(
            MunicipalProfile.organization_id == organization_id
        )
    )


def read_profile(profile: MunicipalProfile | None) -> MunicipalProfileRead:
    if profile is None:
        return MunicipalProfileRead()
    return MunicipalProfileRead(
        display_name=profile.display_name,
        weather_enabled=profile.weather_enabled,
        weather_location=profile.weather_location,
        has_shield=profile.shield_storage_key is not None,
    )


def build_nav(db: Session, organization_id: int) -> list[MunicipalNavSectionRead]:
    """Árbol de navegación activo, ya ordenado por posición.

    Tres niveles visibles —pestaña, epígrafe y apartado—, los mismos que el
    diseño de referencia. Un hijo cuyo padre esté archivado no se muestra:
    archivar la tarjeta se lleva consigo lo que cuelga de ella.
    """
    blocks = list(
        db.scalars(
            select(MunicipalBlock)
            .where(
                MunicipalBlock.organization_id == organization_id,
                MunicipalBlock.status == "active",
                MunicipalBlock.block_type.in_(
                    ("nav_section", "epigraph", "nav_item")
                ),
            )
            .order_by(MunicipalBlock.position, MunicipalBlock.id)
        )
    )

    sections: dict[int, MunicipalNavSectionRead] = {}
    for block in blocks:
        if block.block_type == "nav_section":
            sections[block.id] = MunicipalNavSectionRead(
                id=block.id,
                title=block.title,
                position=block.position,
                epigraphs=[],
            )

    epigraphs: dict[int, MunicipalNavEpigraphRead] = {}
    for block in blocks:
        if block.block_type != "epigraph" or block.parent_id is None:
            continue
        section = sections.get(block.parent_id)
        if section is None:
            continue
        seed_key = read_seed_key(block)
        epigraph = MunicipalNavEpigraphRead(
            id=block.id,
            title=block.title,
            position=block.position,
            module=EPIGRAPH_MODULES.get(seed_key or ""),
            items=[],
        )
        epigraphs[block.id] = epigraph
        section.epigraphs.append(epigraph)

    for block in blocks:
        if block.block_type != "nav_item" or block.parent_id is None:
            continue
        epigraph = epigraphs.get(block.parent_id)
        if epigraph is None:
            continue
        epigraph.items.append(
            MunicipalNavItemRead(
                id=block.id,
                title=block.title,
                position=block.position,
            )
        )

    return list(sections.values())


def read_layout(block: MunicipalBlock) -> str:
    """Formato guardado en `data_json`; `text` ante cualquier dato ilegible."""
    if not block.data_json:
        return "text"

    try:
        payload = json.loads(block.data_json)
    except ValueError:
        return "text"

    layout = payload.get("layout") if isinstance(payload, dict) else None
    return layout if layout in SECTION_LAYOUTS else "text"


def write_layout(block: MunicipalBlock, layout: str) -> None:
    payload: dict[str, object] = {}
    if block.data_json:
        try:
            loaded = json.loads(block.data_json)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    payload["layout"] = layout
    block.data_json = json.dumps(payload, ensure_ascii=False)


def read_fields(block: MunicipalBlock) -> list[MunicipalContentField]:
    """Campos libres del elemento; lista vacía ante cualquier dato ilegible."""
    if not block.data_json:
        return []

    try:
        payload = json.loads(block.data_json)
    except ValueError:
        return []

    raw = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    fields: list[MunicipalContentField] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        value = entry.get("value", "")
        if isinstance(label, str) and label.strip() and isinstance(value, str):
            fields.append(MunicipalContentField(label=label, value=value))

    return fields


def write_fields(
    block: MunicipalBlock,
    fields: list[MunicipalContentField],
) -> None:
    payload: dict[str, object] = {}
    if block.data_json:
        try:
            loaded = json.loads(block.data_json)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    payload["fields"] = [field.model_dump() for field in fields]
    block.data_json = json.dumps(payload, ensure_ascii=False)


def read_attachments(block: MunicipalBlock) -> list[dict]:
    """Adjuntos crudos del elemento; lista vacía ante cualquier dato ilegible."""
    if not block.data_json:
        return []

    try:
        payload = json.loads(block.data_json)
    except ValueError:
        return []

    raw = payload.get("attachments") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    return [
        entry
        for entry in raw
        if isinstance(entry, dict)
        and isinstance(entry.get("storage_key"), str)
        and isinstance(entry.get("name"), str)
    ]


def write_attachments(block: MunicipalBlock, attachments: list[dict]) -> None:
    payload: dict[str, object] = {}
    if block.data_json:
        try:
            loaded = json.loads(block.data_json)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    payload["attachments"] = attachments
    block.data_json = json.dumps(payload, ensure_ascii=False)


def public_attachments(block: MunicipalBlock) -> list[MunicipalAttachmentRead]:
    """Vista publicable: nunca sale la clave de almacenamiento."""
    return [
        MunicipalAttachmentRead(
            index=index,
            name=entry["name"],
            content_type=entry.get("content_type", "application/octet-stream"),
            size_bytes=entry.get("size_bytes", 0),
        )
        for index, entry in enumerate(read_attachments(block))
    ]


def require_parent_of(db: Session, item: MunicipalBlock) -> MunicipalBlock:
    """Apartado al que pertenece un elemento, para reconstruir su contenido."""
    parent = (
        db.get(MunicipalBlock, item.parent_id)
        if item.parent_id is not None
        else None
    )
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Block not found",
        )
    return parent


def build_content(db: Session, block: MunicipalBlock) -> MunicipalContentRead:
    """Contenido publicable de un apartado, ya ordenado."""
    items = db.scalars(
        select(MunicipalBlock)
        .where(
            MunicipalBlock.parent_id == block.id,
            MunicipalBlock.status == "active",
            MunicipalBlock.block_type == "item",
        )
        .order_by(MunicipalBlock.position, MunicipalBlock.id)
    )
    parent = (
        db.get(MunicipalBlock, block.parent_id)
        if block.parent_id is not None
        else None
    )

    return MunicipalContentRead(
        block_id=block.id,
        title=block.title,
        parent_title=parent.title if parent is not None else None,
        layout=read_layout(block),  # type: ignore[arg-type]
        items=[
            MunicipalContentItemRead(
                id=item.id,
                title=item.title,
                body=item.body,
                position=item.position,
                fields=read_fields(item),
                attachments=public_attachments(item),
                points=read_points(item),
            )
            for item in items
        ],
    )


def read_points(block: MunicipalBlock) -> list[MunicipalSeriesPoint]:
    """Puntos de la serie; lista vacía ante cualquier dato ilegible."""
    if not block.data_json:
        return []

    try:
        payload = json.loads(block.data_json)
    except ValueError:
        return []

    raw = payload.get("points") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    points: list[MunicipalSeriesPoint] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        x = entry.get("x")
        y = entry.get("y")
        if isinstance(x, str) and x.strip() and isinstance(y, (int, float)):
            points.append(MunicipalSeriesPoint(x=x, y=float(y)))

    return points


def write_points(block: MunicipalBlock, points: list[MunicipalSeriesPoint]) -> None:
    payload: dict[str, object] = {}
    if block.data_json:
        try:
            loaded = json.loads(block.data_json)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    payload["points"] = [point.model_dump() for point in points]
    block.data_json = json.dumps(payload, ensure_ascii=False)


def archive_descendants(
    db: Session,
    block: MunicipalBlock,
    actor_id: int,
) -> None:
    """Archiva en cascada: un apartado se lleva sus elementos por delante."""
    children = list(
        db.scalars(
            select(MunicipalBlock).where(
                MunicipalBlock.parent_id == block.id,
                MunicipalBlock.status == "active",
            )
        )
    )

    for child in children:
        child.status = "archived"
        child.updated_by_id = actor_id
        archive_descendants(db, child, actor_id)


def get_owned_block(db: Session, block_id: int) -> MunicipalBlock:
    block = db.get(MunicipalBlock, block_id)
    if block is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Block not found",
        )
    return block


def next_position(db: Session, organization_id: int, parent_id: int | None) -> int:
    query = select(func.max(MunicipalBlock.position)).where(
        MunicipalBlock.organization_id == organization_id,
        MunicipalBlock.status == "active",
    )
    if parent_id is None:
        query = query.where(MunicipalBlock.parent_id.is_(None))
    else:
        query = query.where(MunicipalBlock.parent_id == parent_id)

    highest = db.scalar(query)
    return 0 if highest is None else highest + 1


def require_parent_block(
    db: Session,
    organization_id: int,
    parent_id: int,
    expected_type: str,
) -> MunicipalBlock:
    parent = db.get(MunicipalBlock, parent_id)
    if (
        parent is None
        or parent.organization_id != organization_id
        or parent.block_type != expected_type
        or parent.status != "active"
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Parent must be an active navigation section of the same organization",
        )
    return parent


def resolve_parent(
    db: Session,
    organization_id: int,
    block_type: str,
    parent_id: int | None,
) -> int | None:
    """Aplica la jerarquía pestaña → epígrafe → apartado → elemento."""
    expected_type = BLOCK_PARENT_TYPES[block_type]

    if expected_type is None:
        if parent_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A navigation section cannot have a parent",
            )
        return None

    if parent_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A navigation item requires a parent section",
        )

    return require_parent_block(db, organization_id, parent_id, expected_type).id


@router.get("", response_model=TownHallRead)
def read_town_hall(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> TownHallRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_view(db, current_user, organization.id)

    return TownHallRead(
        organization_id=organization.id,
        organization_name=organization.name,
        profile=read_profile(get_profile(db, organization.id)),
        nav=build_nav(db, organization.id),
    )


@router.patch("/profile", response_model=MunicipalProfileRead)
def update_profile(
    payload: MunicipalProfileUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> MunicipalProfileRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    profile = get_profile(db, organization.id)
    if profile is None:
        profile = MunicipalProfile(organization_id=organization.id)
        db.add(profile)

    changes = payload.model_dump(exclude_unset=True)
    relocated = (
        "weather_location" in changes
        and changes["weather_location"] != profile.weather_location
    )

    for field, value in changes.items():
        setattr(profile, field, value)
    profile.updated_by_id = current_user.id

    if relocated:
        # Otra localidad, otras coordenadas: se vuelven a geocodificar en la
        # siguiente lectura y la temperatura cacheada deja de valer.
        profile.weather_latitude = None
        profile.weather_longitude = None
        weather.forget_cached_temperature(organization.id)

    db.commit()
    db.refresh(profile)
    return read_profile(profile)


@router.get("/weather", response_model=MunicipalWeatherRead)
def read_weather(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> MunicipalWeatherRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_view(db, current_user, organization.id)

    profile = get_profile(db, organization.id)
    if profile is None or not profile.weather_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Weather block is disabled",
        )

    location = (
        profile.weather_location
        or profile.display_name
        or organization.name
    )

    cached = weather.get_cached_temperature(organization.id)
    if cached is not None:
        return MunicipalWeatherRead(temperature_celsius=cached, location=location)

    try:
        if profile.weather_latitude is None or profile.weather_longitude is None:
            # Se geocodifica una sola vez y se guarda: las cargas siguientes ya
            # no vuelven a preguntar por el nombre del municipio.
            coordinates = weather.geocode(location)
            profile.weather_latitude = coordinates.latitude
            profile.weather_longitude = coordinates.longitude
            db.commit()
        else:
            coordinates = weather.Coordinates(
                latitude=profile.weather_latitude,
                longitude=profile.weather_longitude,
            )

        temperature = weather.fetch_temperature(coordinates)
    except weather.WeatherUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Weather provider unavailable",
        ) from None

    weather.cache_temperature(organization.id, temperature)
    return MunicipalWeatherRead(temperature_celsius=temperature, location=location)


@router.post("/shield", response_model=MunicipalProfileRead)
def upload_shield(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> MunicipalProfileRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)
    require_rate_limit_slot(
        upload_rate_limiter,
        str(current_user.id),
        detail="Too many uploads",
    )

    try:
        stored_upload = storage_service.save_branding_file(
            file,
            organization_id=organization.id,
            max_bytes=settings.municipal_shield_max_upload_bytes,
        )
    except UnsupportedDocumentContentTypeError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported shield content type",
        ) from None
    except DocumentTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Shield exceeds maximum upload size",
        ) from None
    except EmptyDocumentError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty shield upload",
        ) from None
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid shield storage key",
        ) from None

    profile = get_profile(db, organization.id)
    if profile is None:
        profile = MunicipalProfile(organization_id=organization.id)
        db.add(profile)

    previous_key = profile.shield_storage_key
    profile.shield_storage_key = stored_upload.storage_key
    profile.shield_content_type = stored_upload.content_type
    profile.shield_size_bytes = stored_upload.size_bytes
    profile.updated_by_id = current_user.id

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        storage_service.delete_file(stored_upload.storage_key)
        raise

    # El escudo anterior deja de ser alcanzable: se borra tras confirmar el
    # cambio para no dejar huérfanos en el volumen.
    if previous_key is not None and previous_key != stored_upload.storage_key:
        storage_service.delete_file(previous_key)

    db.refresh(profile)
    return read_profile(profile)


@router.get("/shield")
def download_shield(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> FileResponse:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_view(db, current_user, organization.id)

    profile = get_profile(db, organization.id)
    if profile is None or profile.shield_storage_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shield not found",
        )

    try:
        file_path = storage_service.resolve_storage_key(profile.shield_storage_key)
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shield not found",
        ) from None

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shield not found",
        )

    # El escudo se sirve siempre como descarga, nunca inline. Un SVG servido
    # inline se renderiza como documento en el origen de la API y puede
    # ejecutar scripts con la cookie de sesión en alcance; dentro de un <img>
    # (que es como lo pinta el frontend) no puede. Content-Disposition solo
    # afecta a la navegación de primer nivel, así que la insignia sigue
    # mostrándose con normalidad. Ver ADR-036.
    #
    # La extensión sale del content-type validado y no del nombre original: la
    # del fichero almacenado la elige quien sube (build_stored_filename arrastra
    # el sufijo del cliente), así que usarla dejaría la cabecera bajo su control.
    content_type = profile.shield_content_type or "application/octet-stream"
    suffix = DEFAULT_EXTENSIONS_BY_CONTENT_TYPE.get(content_type, "")
    return FileResponse(
        file_path,
        media_type=content_type,
        filename=f"escudo{suffix}",
        content_disposition_type="attachment",
    )


def require_content_item(db: Session, block_id: int) -> MunicipalBlock:
    block = get_owned_block(db, block_id)
    if block.block_type != "item" or block.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Block not found",
        )
    return block


@router.post("/blocks/{block_id}/attachments", response_model=MunicipalContentRead)
def upload_attachment(
    block_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
) -> MunicipalContentRead:
    block = require_content_item(db, block_id)
    require_town_hall_edit(db, current_user, block.organization_id)
    require_rate_limit_slot(
        upload_rate_limiter,
        str(current_user.id),
        detail="Too many uploads",
    )

    attachments = read_attachments(block)
    if len(attachments) >= MAX_ITEM_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Too many attachments",
        )

    try:
        stored_upload = storage_service.save_archive_file(
            file,
            organization_id=block.organization_id,
            max_bytes=settings.municipal_attachment_max_upload_bytes,
        )
    except UnsupportedDocumentContentTypeError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported attachment content type",
        ) from None
    except DocumentTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Attachment exceeds maximum upload size",
        ) from None
    except EmptyDocumentError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty attachment upload",
        ) from None
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid attachment storage key",
        ) from None

    attachments.append(
        {
            "storage_key": stored_upload.storage_key,
            "name": stored_upload.original_filename,
            "content_type": stored_upload.content_type,
            "size_bytes": stored_upload.size_bytes,
        }
    )
    write_attachments(block, attachments)
    block.updated_by_id = current_user.id

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        storage_service.delete_file(stored_upload.storage_key)
        raise

    return build_content(db, require_parent_of(db, block))


@router.get("/blocks/{block_id}/attachments/{index}")
def download_attachment(
    block_id: int,
    index: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FileResponse:
    block = require_content_item(db, block_id)
    require_town_hall_view(db, current_user, block.organization_id)

    attachments = read_attachments(block)
    if index < 0 or index >= len(attachments):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )

    entry = attachments[index]
    try:
        file_path = storage_service.resolve_storage_key(entry["storage_key"])
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        ) from None

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )

    # Siempre como descarga, por la misma razón que el escudo (ADR-036): un
    # fichero servido inline se renderiza en el origen de la API. El nombre sale
    # del content-type validado, no del que eligió quien subió.
    content_type = entry.get("content_type", "application/octet-stream")
    suffix = DEFAULT_EXTENSIONS_BY_CONTENT_TYPE.get(content_type, "")
    return FileResponse(
        file_path,
        media_type=content_type,
        filename=f"adjunto-{index + 1}{suffix}",
        content_disposition_type="attachment",
    )


@router.delete(
    "/blocks/{block_id}/attachments/{index}",
    response_model=MunicipalContentRead,
)
def delete_attachment(
    block_id: int,
    index: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalContentRead:
    block = require_content_item(db, block_id)
    require_town_hall_edit(db, current_user, block.organization_id)

    attachments = read_attachments(block)
    if index < 0 or index >= len(attachments):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )

    removed = attachments.pop(index)
    write_attachments(block, attachments)
    block.updated_by_id = current_user.id
    db.commit()

    # El fichero se borra después de confirmar: si el commit falla, sigue ahí.
    storage_service.delete_file(removed["storage_key"])

    return build_content(db, require_parent_of(db, block))


@router.post(
    "/structure/seed",
    response_model=TownHallStructureSeedResult,
    status_code=status.HTTP_201_CREATED,
)
def seed_town_hall_structure(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> TownHallStructureSeedResult:
    """Siembra las pestañas y apartados de partida del Ayuntamiento.

    Bajo petición y no al arrancar: la estructura es un punto de partida que
    cada ayuntamiento adapta, no un catálogo del producto. Repetir la llamada no
    deshace nada — sólo se crea lo que falte. No siembra contenido municipal.
    """
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    created = ensure_initial_town_hall_structure(
        db,
        organization.id,
        created_by_id=current_user.id,
    )
    return TownHallStructureSeedResult(
        organization_id=organization.id,
        created=created,
    )


@router.post(
    "/blocks",
    response_model=MunicipalBlockRead,
    status_code=status.HTTP_201_CREATED,
)
def create_block(
    payload: MunicipalBlockCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> MunicipalBlockRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    parent_id = resolve_parent(
        db,
        organization.id,
        payload.block_type,
        payload.parent_id,
    )

    block = MunicipalBlock(
        organization_id=organization.id,
        parent_id=parent_id,
        block_type=payload.block_type,
        title=payload.title,
        position=next_position(db, organization.id, parent_id),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(block)
    db.commit()
    db.refresh(block)
    return MunicipalBlockRead.model_validate(block)


@router.post("/blocks/reorder", response_model=list[MunicipalBlockRead])
def reorder_blocks(
    payload: MunicipalBlockReorder,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[MunicipalBlockRead]:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    placements = {placement.id: placement for placement in payload.placements}
    if len(placements) != len(payload.placements):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Duplicate block in reorder payload",
        )

    blocks = list(
        db.scalars(
            select(MunicipalBlock).where(MunicipalBlock.id.in_(placements.keys()))
        )
    )
    if len(blocks) != len(placements):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Block not found",
        )

    for block in blocks:
        # La comprobación por bloque cierra la fuga entre organizaciones: no
        # basta con tener permiso en la organización resuelta.
        if block.organization_id != organization.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Block not found",
            )

        placement = placements[block.id]
        block.parent_id = resolve_parent(
            db,
            organization.id,
            block.block_type,
            placement.parent_id,
        )
        block.position = placement.position
        block.updated_by_id = current_user.id

    db.commit()
    return [MunicipalBlockRead.model_validate(block) for block in blocks]


@router.get("/blocks/{block_id}/content", response_model=MunicipalContentRead)
def read_block_content(
    block_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalContentRead:
    block = get_owned_block(db, block_id)
    require_town_hall_view(db, current_user, block.organization_id)

    if block.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Block not found",
        )

    return build_content(db, block)


@router.patch("/blocks/{block_id}", response_model=MunicipalBlockRead)
def update_block(
    block_id: int,
    payload: MunicipalBlockUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalBlockRead:
    block = get_owned_block(db, block_id)
    require_town_hall_edit(db, current_user, block.organization_id)

    fields = payload.model_dump(exclude_unset=True)
    if "title" in fields:
        block.title = fields["title"]
    if "body" in fields:
        if block.block_type != "item":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Only content items carry a body",
            )
        body = fields["body"]
        block.body = body.strip() or None if body is not None else None
    if "layout" in fields:
        if block.block_type != "nav_item":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Only sections carry a layout",
            )
        write_layout(block, fields["layout"])
    if "fields" in fields:
        if block.block_type != "item":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Only content items carry fields",
            )
        write_fields(block, payload.fields or [])
    if "points" in fields:
        if block.block_type != "item":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Only content items carry a series",
            )
        write_points(block, payload.points or [])
    if "status" in fields:
        block.status = fields["status"]
        # Archivar arrastra la descendencia: preferimos el borrado lógico al
        # físico, pero sin dejar bloques huérfanos reordenables.
        if block.status == "archived":
            archive_descendants(db, block, current_user.id)

    block.updated_by_id = current_user.id
    db.commit()
    db.refresh(block)
    return MunicipalBlockRead.model_validate(block)
