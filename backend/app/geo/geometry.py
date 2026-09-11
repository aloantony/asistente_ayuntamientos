import json
import math

from fastapi import HTTPException, status


def validate_point_coordinates(latitude: float, longitude: float) -> None:
    if not math.isfinite(latitude) or latitude < -90 or latitude > 90:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Latitude must be between -90 and 90",
        )
    if not math.isfinite(longitude) or longitude < -180 or longitude > 180:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Longitude must be between -180 and 180",
        )


def build_point_geojson(latitude: float, longitude: float) -> str:
    """Build an RFC 7946 GeoJSON Point string using [longitude, latitude]."""
    validate_point_coordinates(latitude, longitude)
    return json.dumps(
        {"type": "Point", "coordinates": [longitude, latitude]},
        separators=(",", ":"),
    )


def validated_geometry(value: dict) -> tuple[str, dict, float, float]:
    """Bounded WGS84 Point/LineString/simple Polygon; no implicit CRS or holes."""
    import math
    kind = value.get('type')
    coordinates = value.get('coordinates')
    if set(value) != {'type', 'coordinates'} or kind not in ('Point', 'LineString', 'Polygon'):
        raise ValueError('Use Point, LineString or Polygon in WGS84')
    points = [coordinates] if kind == 'Point' else coordinates
    if kind == 'Polygon':
        if not isinstance(coordinates, list) or len(coordinates) != 1:
            raise ValueError('A basic surface must have one exterior ring')
        points = coordinates[0]
    if not isinstance(points, list) or not 1 <= len(points) <= 200:
        raise ValueError('Geometry must have at most 200 vertices')
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError('Coordinates must be [longitude, latitude]')
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in point):
            raise ValueError('Coordinates must be finite numbers')
        if not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90:
            raise ValueError('Coordinates outside WGS84 bounds')
    if kind == 'LineString' and (len(points) < 2 or len(set(map(tuple, points))) < 2):
        raise ValueError('A line needs two different vertices')
    if kind == 'Polygon':
        if len(points) < 4 or points[0] != points[-1] or len(set(map(tuple, points[:-1]))) != len(points)-1:
            raise ValueError('A surface needs a closed ring with distinct vertices')
        area = sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points, points[1:]))
        if abs(area) < 1e-12:
            raise ValueError('Surface area must be nonzero')
        def cross(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        def on_segment(a,b,c):
            return min(a[0],b[0]) <= c[0] <= max(a[0],b[0]) and min(a[1],b[1]) <= c[1] <= max(a[1],b[1])
        def intersects(a,b,c,d):
            x,y,z,w = cross(a,b,c),cross(a,b,d),cross(c,d,a),cross(c,d,b)
            return (x*y<0 and z*w<0) or any((v==0 and on_segment(p,q,r)) for v,p,q,r in ((x,a,b,c),(y,a,b,d),(z,c,d,a),(w,c,d,b)))
        edges=list(zip(points,points[1:]))
        for i,(a,b) in enumerate(edges):
            for j in range(i+2,len(edges)):
                if i==0 and j==len(edges)-1: continue
                if intersects(a,b,*edges[j]): raise ValueError('Surface edges must not intersect')
    # The first vertex is a stable anchor; display the complete geometry on the map.
    return {'Point':'point','LineString':'line','Polygon':'polygon'}[kind], value, points[0][1], points[0][0]


def location_geometry_json(payload) -> str:
    if payload.geometry is not None:
        return json.dumps(payload.geometry, separators=(',', ':'), allow_nan=False)
    return build_point_geojson(payload.latitude, payload.longitude)
