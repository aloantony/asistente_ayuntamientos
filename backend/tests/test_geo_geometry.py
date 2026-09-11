import json

import pytest
from pydantic import ValidationError

from app.geo.schemas import GeoLocationCreate
from conftest import headers_for


@pytest.mark.parametrize('geometry', [
    {'type': 'Point', 'coordinates': [float('nan'), 42]},
    {'type': 'Point', 'coordinates': [True, 42]},
    {'type': 'Point', 'coordinates': [-3, 91]},
    {'type': 'LineString', 'coordinates': [[-3, 42], [-3, 42]]},
    {'type': 'LineString', 'coordinates': [[-3, 42]] * 201},
    {'type': 'Polygon', 'coordinates': [[[0, 0], [2, 0], [1, 0], [0, 0]]]},
    {'type': 'Polygon', 'coordinates': [[[0, 0], [3, 3], [0, 2], [2, 0], [0, 0]]]},
    {'type': 'Polygon', 'coordinates': [[[0, 0], [2, 0], [2, 2], [0, 2]]]},
    {'type': 'Point', 'coordinates': [-3, 42], 'crs': 'other'},
    {'type': 'MultiPolygon', 'coordinates': []},
])
def test_rejects_invalid_or_unbounded_geometry(geometry):
    with pytest.raises(ValidationError):
        GeoLocationCreate(label='Invalid geometry', geometry=geometry)


@pytest.mark.parametrize('geometry, kind', [
    ({'type': 'LineString', 'coordinates': [[-3, 42], [-3.01, 42.01]]}, 'line'),
    ({'type': 'Polygon', 'coordinates': [[[-3, 42], [-3.01, 42], [-3.01, 42.01], [-3, 42]]]}, 'polygon'),
])
def test_registration_persists_complete_geometry(client, make_user, make_organization, grant_permissions, geometry, kind):
    from uuid import uuid4
    user, org = make_user(), make_organization()
    grant_permissions(user, org, ['map.view', 'map.edit', 'requirements.view', 'requirements.create'])
    response = client.post('/geo/registrations', headers=headers_for(user), json={
        'request_key': str(uuid4()), 'entity_type': 'requirement', 'organization_id': org.id,
        'title': 'Geometría municipal', 'location': {'label': 'Zona', 'geometry': geometry},
    })
    assert response.status_code == 201, response.text
    location = response.json()['location']
    assert location['geometry_type'] == kind
    assert json.loads(location['geometry_json']) == geometry
    assert (location['longitude'], location['latitude']) == (-3, 42)
    listing = client.get('/geo/map-items', headers=headers_for(user), params={'organization_id': org.id})
    assert listing.status_code == 200, listing.text
    assert any(item['entity_id'] == response.json()['entity_id'] and
               json.loads(item['location']['geometry_json']) == geometry for item in listing.json())
