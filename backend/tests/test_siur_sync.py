import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.reference_layers import siur_sync
from app.reference_layers.siur_sync import (
    SiurSyncInputError,
    _baseline_from_args,
    _hash_issue,
    _read_bounded,
    main,
)
from app.reference_layers.siur_wmc import parse_wmc_evidence


WMC_FIXTURE = Path(__file__).parent / "fixtures" / "siur_context.xml"


def matching_documents() -> tuple[bytes, bytes]:
    settings = {
        "services": {
            "urbanismo": {
                "serviceType": "WMS",
                "serviceUrl": (
                    "https://idecyl.jcyl.es/geoserver/urbanismo/wms"
                ),
            }
        },
        "layerGroups": [
            {
                "key": "planning",
                "label": "Planeamiento",
                "layers": [
                    {
                        "key": "classification",
                        "label": "Clasificación",
                        "serviceId": "urbanismo",
                        "layerName": (
                            "urbanismo:plau_cyl_clasificacion"
                        ),
                        "styleName": (
                            "urbanismo:plau_cyl_clasificacion_color"
                        ),
                    }
                ],
            }
        ],
    }
    wmc = """<?xml version="1.0" encoding="utf-8"?>
<ViewContext version="1.1.0" xmlns="http://www.opengis.net/context"
 xmlns:xlink="http://www.w3.org/1999/xlink"
 xmlns:sld="http://www.opengis.net/sld">
  <General>
    <BoundingBox SRS="EPSG:25830" minx="1" miny="1" maxx="2" maxy="2"/>
    <Window width="100" height="100"/>
    <Title>SIUR test</Title>
  </General>
  <LayerList>
    <Layer hidden="0" queryable="1">
      <Server service="OGC:WMS" version="1.1.1">
        <OnlineResource xlink:href="https://idecyl.jcyl.es/geoserver/urbanismo/wms?"/>
      </Server>
      <Name>plau_cyl_clasificacion</Name>
      <Title>Clasificación</Title>
      <SRS>EPSG:25830</SRS>
      <Extension>
        <Opacity>100</Opacity>
        <Extent SRS="EPSG:25830" minx="1" miny="1" maxx="2" maxy="2"/>
      </Extension>
      <StyleList>
        <Style current="1">
          <Name>urbanismo:plau_cyl_clasificacion_color</Name>
          <Title>Clasificación por color</Title>
        </Style>
      </StyleList>
    </Layer>
  </LayerList>
</ViewContext>""".encode()
    return (
        json.dumps(settings, separators=(",", ":")).encode(),
        wmc,
    )


def namespace(**updates) -> argparse.Namespace:
    values = {
        "top_level_groups": None,
        "groups": None,
        "layers": None,
        "services": None,
        "approved_sha256": None,
        "layer_manifest": None,
    }
    values.update(updates)
    return argparse.Namespace(**values)


def test_baseline_arguments_are_all_or_nothing(tmp_path) -> None:
    assert _baseline_from_args(namespace()) is None

    with pytest.raises(SiurSyncInputError, match="cuatro conteos"):
        _baseline_from_args(namespace(layers=224))

    manifest = tmp_path / "layers.json"
    manifest.write_text('["layer:siur:approved"]', encoding="utf-8")
    baseline = _baseline_from_args(
        namespace(
            top_level_groups=12,
            groups=35,
            layers=224,
            services=7,
            approved_sha256="a" * 64,
            layer_manifest=manifest,
        )
    )
    assert baseline.layers == 224
    assert baseline.raw_sha256 == "a" * 64
    assert baseline.layer_keys == {"layer:siur:approved"}


def test_wmc_hash_must_be_explicitly_approved() -> None:
    evidence = parse_wmc_evidence(WMC_FIXTURE.read_bytes().rstrip(b"\r\n"))

    assert _hash_issue(evidence, None) == (
        "WMC approved SHA-256 is required"
    )
    assert _hash_issue(evidence, "not-a-hash") == (
        "WMC approved SHA-256 is invalid"
    )
    assert _hash_issue(evidence, "a" * 64) == (
        "WMC approved SHA-256 does not match"
    )
    assert _hash_issue(evidence, evidence.content_sha256) is None


def test_bounded_file_reader_rejects_empty_or_oversized_files(tmp_path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(SiurSyncInputError, match="tamaño"):
        _read_bounded(empty, 10, "settings.json")

    large = tmp_path / "large.json"
    large.write_bytes(b"{}")
    with pytest.raises(SiurSyncInputError, match="tamaño"):
        _read_bounded(large, 1, "settings.json")


def test_cli_is_a_blocked_dry_run_without_reviewed_baseline(
    tmp_path,
    capsys,
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_bytes(json.dumps({"layerGroups": []}).encode())

    exit_code = main(
        [
            "--settings",
            str(settings),
            "--wmc",
            str(WMC_FIXTURE),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["mode"] == "dry-run"
    assert output["applied"] is False
    assert output["settings"]["unresolved"] >= 1
    assert output["blocking_issues"]


def test_cli_requires_reviewed_definition_and_plan_before_apply(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    settings_document, wmc_document = matching_documents()
    settings = tmp_path / "settings.json"
    wmc = tmp_path / "context.xml"
    manifest = tmp_path / "layers.json"
    settings.write_bytes(settings_document)
    wmc.write_bytes(wmc_document)

    discovery_code = main(
        ["--settings", str(settings), "--wmc", str(wmc)]
    )
    discovery = json.loads(capsys.readouterr().out)
    assert discovery_code == 3
    manifest.write_text(
        json.dumps(discovery["settings"]["layer_keys"]),
        encoding="utf-8",
    )

    class FakeSession:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    plan = SimpleNamespace(
        content_sha256="c" * 64,
        definition_sha256="d" * 64,
        new_services=("service:wms:idecyl:urbanismo",),
        updated_services=(),
        missing_services=(),
        new_layers=(discovery["settings"]["layer_keys"][0],),
        updated_layers=(),
        missing_layers=(),
        new_styles=("layer|style",),
        updated_styles=(),
        missing_styles=(),
        unchanged_count=0,
        blocking_issues=(),
    )
    applied_definitions = []
    monkeypatch.setattr(siur_sync, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        siur_sync,
        "build_catalog_sync_plan",
        lambda db, definition: plan,
    )
    monkeypatch.setattr(
        siur_sync,
        "apply_catalog_definition",
        lambda db, definition, expected_plan: applied_definitions.append(
            definition
        ),
    )

    reviewed_args = [
        "--settings",
        str(settings),
        "--wmc",
        str(wmc),
        "--top-level-groups",
        "1",
        "--groups",
        "1",
        "--layers",
        "1",
        "--services",
        "1",
        "--approved-sha256",
        hashlib.sha256(settings_document).hexdigest(),
        "--approved-wmc-sha256",
        hashlib.sha256(wmc_document).hexdigest(),
        "--layer-manifest",
        str(manifest),
    ]
    review_code = main(reviewed_args)
    review = json.loads(capsys.readouterr().out)
    assert review_code == 3
    assert review["wmc"]["matched_layers"] == 1
    assert review["wmc"]["matched_styles"] == 1
    assert review["plan"]["definition_sha256"] == "d" * 64
    assert applied_definitions == []

    approved_args = reviewed_args + [
        "--approved-definition-sha256",
        review["plan"]["definition_sha256"],
        "--approved-plan-sha256",
        review["plan"]["plan_sha256"],
    ]
    clean_code = main(approved_args)
    clean = json.loads(capsys.readouterr().out)
    assert clean_code == 0
    assert clean["ok"] is True
    assert clean["applied"] is False
    assert applied_definitions == []

    apply_code = main(approved_args + ["--apply"])
    applied = json.loads(capsys.readouterr().out)
    assert apply_code == 0
    assert applied["applied"] is True
    assert len(applied_definitions) == 1
    definition = applied_definitions[0]
    layer = next(item for item in definition.layers if item.node_type == "layer")
    assert layer.style_name == "urbanismo:plau_cyl_clasificacion_color"
    assert len(layer.styles) == 1

    bad_plan_args = approved_args[:-1] + ["0" * 64, "--apply"]
    blocked_code = main(bad_plan_args)
    blocked = json.loads(capsys.readouterr().out)
    assert blocked_code == 3
    assert blocked["applied"] is False
    assert len(applied_definitions) == 1
