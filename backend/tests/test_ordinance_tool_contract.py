import json

import pytest
from app.assistant import tools as assistant_tools


def _province_row(index: int) -> dict:
    return {
        "province": f"Provincia {index:02d}",
        "municipalities_in_scope": 100 + index,
        "municipalities_without_population": index % 3,
        "eligible_municipalities": 90 + index,
        "catalog_municipalities": 80 + index,
        "catalog_ordinances": 200 + index,
        "total_chunks": 1_000 + index,
        "searchable_chunks": 900 + index,
    }


def test_large_manifest_uses_lossless_province_row_table():
    payload = {
        "snapshot_id": "a" * 64,
        "catalog_cursor": "signed.cursor",
        "filters": {},
        "layers": {"catalog_ordinances": 1_800},
        "reconciliation": {"ordinance_partition_balanced": True},
        "completeness": {"manifest_counts_complete": True},
        "by_province": [_province_row(index) for index in range(200)],
    }

    assert len(json.dumps(payload, ensure_ascii=False)) > (
        assistant_tools.MAX_ORDINANCE_TOOL_RESULT_CHARS
    )
    serialized = assistant_tools._serialize_ordinance_manifest_payload(payload)
    compact = json.loads(serialized)

    assert len(serialized) < assistant_tools.MAX_ORDINANCE_TOOL_RESULT_CHARS
    assert compact["snapshot_id"] == payload["snapshot_id"]
    assert compact["catalog_cursor"] == payload["catalog_cursor"]
    assert compact["reconciliation"] == payload["reconciliation"]
    assert compact["completeness"] == payload["completeness"]
    assert compact["payload_compacted"] is True
    table = compact["by_province"]
    assert table["format"] == "row_table"
    assert len(table["rows"]) == len(payload["by_province"])
    reconstructed = [
        dict(zip(table["columns"], row, strict=True)) for row in table["rows"]
    ]
    assert reconstructed == payload["by_province"]


@pytest.mark.parametrize(
    "tool_input, message",
    [
        ({"unexpected": True}, "campos no permitidos"),
        ({"municipality_id": True}, "municipality_id debe ser un entero"),
        ({"population_gte": 5_000, "population_lt": 5_000}, "debe ser menor"),
        ({"province": "x" * 256}, "province no puede superar"),
        ({"include_pending": "yes"}, "include_pending debe ser booleano"),
    ],
)
def test_manifest_input_validation_is_server_side(tool_input, message):
    with pytest.raises(ValueError, match=message):
        assistant_tools._validate_ordinance_manifest_tool_input(tool_input)


@pytest.mark.parametrize(
    "tool_input, message",
    [
        ({}, "cursor es obligatorio"),
        ({"cursor": "valid", "limit": 0}, "limit debe ser un entero entre"),
        ({"cursor": "valid", "limit": 11}, "limit debe ser un entero entre"),
        ({"cursor": "valid", "limit": True}, "limit debe ser un entero entre"),
        ({"cursor": "valid", "extra": 1}, "campos no permitidos"),
    ],
)
def test_catalog_input_validation_is_server_side(tool_input, message):
    with pytest.raises(ValueError, match=message):
        assistant_tools._validate_ordinance_catalog_tool_input(tool_input)
