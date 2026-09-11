from sqlalchemy import select, func
from app.assets.models import MunicipalAsset, MunicipalAssetCategory, MunicipalAssetType
from app.maintenance.models import MaintenanceOrder
from app.tasks.models import MunicipalTask
from app.municipalities.models import Municipality
from app.assistant import tools

NAMES = frozenset(t['name'] for t in __import__('app.assistant.municipal_tools', fromlist=['MUNICIPAL_TOOLS']).MUNICIPAL_TOOLS if 'normalizer' in t)


def probe(db, make_user, make_organization, name):
    user = make_user(is_superuser=True)
    org = make_organization()
    municipality = Municipality(name='Municipio ficticio', province='Burgos', autonomous_community='Castilla y León')
    db.add(municipality); db.flush()
    org.municipality_id = municipality.id
    category = MunicipalAssetCategory(organization_id=org.id, code='test', name='Pruebas')
    db.add(category); db.flush()
    kind = MunicipalAssetType(organization_id=org.id, category_id=category.id, code='test', name='Banco')
    db.add(kind); db.flush()
    asset = MunicipalAsset(organization_id=org.id, municipality_id=municipality.id, asset_type_id=kind.id, name='Banco inicial')
    task = MunicipalTask(organization_id=org.id, title='Tarea inicial')
    db.add_all([asset, task]); db.flush()
    order = MaintenanceOrder(organization_id=org.id, municipality_id=municipality.id, asset_id=asset.id,
        title='Orden inicial', created_by_id=user.id, updated_by_id=user.id)
    db.add(order); db.commit()
    probes = {
        'create_municipal_asset': {'organization_id':org.id,'asset_type_id':kind.id,'name':'Banco nuevo'},
        'update_municipal_asset': {'entity_id':asset.id,'changes':{'name':'Banco actualizado'}},
        'create_municipal_task': {'organization_id':org.id,'title':'Tarea nueva'},
        'update_municipal_task': {'entity_id':task.id,'changes':{'title':'Tarea actualizada'}},
        'transition_municipal_task': {'entity_id':task.id,'changes':{'status':'in_progress'}},
        'create_maintenance_order': {'asset_id':asset.id,'title':'Orden nueva'},
        'update_maintenance_order': {'entity_id':order.id,'changes':{'title':'Orden actualizada'}},
        'transition_maintenance_order': {'entity_id':order.id,'changes':{'status':'in_progress'}},
    }
    payload = probes[name]
    expected = tools.normalize_tool_input(db,user,name,payload)
    return user,payload,expected


def signature(db,name,payload):
    domain = tools.TOOL_CATALOG[name].domain
    model = {'assets':MunicipalAsset,'tasks':MunicipalTask,'maintenance':MaintenanceOrder}[domain]
    if name.startswith('create_'):
        column = model.name if domain=='assets' else model.title
        return db.scalar(select(func.count()).select_from(model).where(column == payload.get('name',payload.get('title'))))
    record=db.get(model,payload['entity_id'])
    return (getattr(record,'name',getattr(record,'title',None)),record.status)
