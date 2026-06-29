import json

from fastapi import HTTPException, status


def validate_point_coordinates(latitude: float, longitude: float) -> None:
    if latitude < -90 or latitude > 90:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Latitude must be between -90 and 90",
        )
    if longitude < -180 or longitude > 180:
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
