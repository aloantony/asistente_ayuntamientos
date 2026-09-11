"""Municipal tools share HTTP domain services and the outer approval transaction."""
import hashlib
import json
from functools import partial
from typing import Literal

from fastapi import HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select

from app.assets import routes as assets
from app.assets import schemas as asset_schemas
from app.assets.models import MunicipalAsset
from app.tasks import routes as tasks
from app.tasks import schemas as task_schemas
from app.tasks.models import MunicipalTask
from app.maintenance import routes as maintenance
from app.maintenance import schemas as maintenance_schemas
from app.maintenance.models import MaintenanceOrder
from app.core.pagination import PageParams


Domain = Literal['assets', 'tasks', 'maintenance']


class MunicipalQuery(BaseModel):
    domain: Literal['assets', 'tasks', 'maintenance', 'asset_types']
    organization_id: int = Field(gt=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    model_config = ConfigDict(extra='forbid')


class MunicipalIdentity(BaseModel):
    domain: Domain
    entity_id: int = Field(gt=0)
    model_config = ConfigDict(extra='forbid')


class AssetUpdateInput(BaseModel):
    entity_id: int = Field(gt=0)
    changes: asset_schemas.MunicipalAssetUpdate
    model_config = ConfigDict(extra='forbid')


class TaskUpdateInput(BaseModel):
    entity_id: int = Field(gt=0)
    changes: task_schemas.MunicipalTaskUpdate
    model_config = ConfigDict(extra='forbid')


class TaskTransitionInput(BaseModel):
    entity_id: int = Field(gt=0)
    changes: task_schemas.MunicipalTaskTransition
    model_config = ConfigDict(extra='forbid')


class MaintenanceUpdateInput(BaseModel):
    entity_id: int = Field(gt=0)
    changes: maintenance_schemas.MaintenanceOrderUpdate
    model_config = ConfigDict(extra='forbid')


class MaintenanceTransitionInput(BaseModel):
    entity_id: int = Field(gt=0)
    changes: maintenance_schemas.MaintenanceOrderTransition
    model_config = ConfigDict(extra='forbid')


MODELS = {'assets': MunicipalAsset, 'tasks': MunicipalTask, 'maintenance': MaintenanceOrder}
READ_SCHEMAS = {'assets': asset_schemas.MunicipalAssetRead, 'tasks': task_schemas.MunicipalTaskDetail,
                'maintenance': maintenance_schemas.MaintenanceOrderDetail}


def read_record(db, user, domain, entity_id):
    if domain == 'assets': return assets.get_asset(entity_id, db, user)
    if domain == 'tasks': return tasks.get_task(entity_id, db, user)
    return maintenance.get_maintenance_order(entity_id, db, user)


def serialize(domain, record):
    result = READ_SCHEMAS[domain].model_validate(record).model_dump(mode='json')
    result['map_path'] = f'/mapa?entity_type=asset&entity_id={record.id}&organization_id={record.organization_id}' if domain == 'assets' else None
    result['detail_path'] = f'/hoja-de-ruta?organization_id={record.organization_id}&task_id={record.id}' if domain == 'tasks' else f'/inventario?organization_id={record.organization_id}&asset_id={record.id if domain == "assets" else record.asset_id}'
    return result


def list_records(db, user, tool_input, context):
    query = MunicipalQuery.model_validate(tool_input)
    response = Response()
    common = dict(organization_id=query.organization_id, db=db, current_user=user,
                  response=response, page=PageParams(limit=query.limit, offset=query.offset))
    if query.domain == 'asset_types':
        records = assets.list_asset_types(**common, status_filter='active')
        return {'domain': query.domain, 'offset': query.offset,
                'total': int(response.headers.get('X-Total-Count', len(records))),
                'items': [{'id': r.id, 'name': r.name, 'category': r.category.name,
                           'category_status': r.category.status, 'organization_id': r.organization_id}
                          for r in records]}
    if query.domain == 'assets': records = assets.list_assets(**common)
    elif query.domain == 'tasks': records = tasks.list_tasks(**common, include_closed=True)
    else: records = maintenance.list_maintenance_orders(**common)
    # Lists omit event history; the detail tool reads it with fresh permissions.
    return {'domain': query.domain, 'offset': query.offset,
            'total': int(response.headers.get('X-Total-Count', len(records))),
            'items': [{'id': r.id, 'organization_id': r.organization_id,
                       'title': getattr(r, 'title', getattr(r, 'name', '')),
                       'status': r.status} for r in records]}


def get_record(db, user, tool_input, context):
    query = MunicipalIdentity.model_validate(tool_input)
    return serialize(query.domain, read_record(db, user, query.domain, query.entity_id))


def normalize(db, user, tool_input, context, *, schema, domain, editing=False):
    raw = {key: value for key, value in tool_input.items() if key != '_expected_version'}
    validated = schema.model_validate(raw)
    result = validated.model_dump(mode='json', exclude_unset=editing)
    if editing:
        record = read_record(db, user, domain, validated.entity_id)
        if context.lock_effects:
            record = db.scalar(select(MODELS[domain]).where(MODELS[domain].id == record.id)
                               .with_for_update().execution_options(populate_existing=True))
        if record is None: raise HTTPException(409, 'Municipal record changed')
        snapshot = {column.key: getattr(record, column.key)
                    for column in inspect(type(record)).column_attrs}
        result['_expected_version'] = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str, ensure_ascii=False).encode('utf-8')
        ).hexdigest()
    return result


def write_record(db, user, tool_input, context, *, schema, domain, operation, editing=False):
    values = schema.model_validate({k:v for k,v in tool_input.items() if k != '_expected_version'})
    if editing:
        record = operation(values.entity_id, values.changes, db, user)
    elif domain == 'assets':
        record = operation(values, db, user, commit=False)
    else:
        record = operation(values, db, user)
    db.flush()
    # Avoid requiring extra read permissions after an authorized creation: the
    # result contains only the identity/status of the object just written.
    return {'domain': domain, 'entity_id': record.id, 'organization_id': record.organization_id,
            'status': record.status, 'title': getattr(record, 'title', getattr(record, 'name', ''))}


MUNICIPAL_TOOLS = [
    {'name':'list_municipal_records','label':'Consultar trabajo municipal','description':'Lista inventario (assets), tipos para clasificar altas (asset_types), tareas humanas (tasks) u órdenes de mantenimiento (maintenance) de la organización. Las tareas humanas son distintas de agent_office. Usa offset para continuar.',
     'schema':MunicipalQuery,'executor':list_records,'domain':'municipal'},
    {'name':'get_municipal_record','label':'Abrir ficha municipal','description':'Lee la ficha actual de un activo, tarea humana u orden de mantenimiento, sus relaciones y su historial disponible. Usa solo identidades verificadas.',
     'schema':MunicipalIdentity,'executor':get_record,'domain':'municipal'},
]

for name,label,domain,schema,operation,editing in [
    ('create_municipal_asset','Crear activo','assets',asset_schemas.MunicipalAssetCreate,assets.create_asset_record,False),
    ('update_municipal_asset','Editar o archivar activo','assets',AssetUpdateInput,assets.update_asset_record,True),
    ('create_municipal_task','Crear tarea humana','tasks',task_schemas.MunicipalTaskCreate,tasks.create_task_record,False),
    ('update_municipal_task','Editar tarea humana','tasks',TaskUpdateInput,tasks.update_task_record,True),
    ('transition_municipal_task','Cambiar estado de tarea humana','tasks',TaskTransitionInput,tasks.transition_task_record,True),
    ('create_maintenance_order','Crear mantenimiento','maintenance',maintenance_schemas.MaintenanceOrderCreate,maintenance.create_maintenance_order_record,False),
    ('update_maintenance_order','Editar mantenimiento','maintenance',MaintenanceUpdateInput,maintenance.update_maintenance_order_record,True),
    ('transition_maintenance_order','Cambiar estado de mantenimiento','maintenance',MaintenanceTransitionInput,maintenance.transition_maintenance_order_record,True),
]:
    MUNICIPAL_TOOLS.append(dict(name=name,label=label,domain=domain,schema=schema,
        description=label+'. Opera sobre los mismos registros que las fichas municipales; requiere confirmación explícita y permisos actuales. No inventes identificadores.',
        executor=partial(write_record,schema=schema,domain=domain,operation=operation,editing=editing),
        normalizer=partial(normalize,schema=schema,domain=domain,editing=editing)))
