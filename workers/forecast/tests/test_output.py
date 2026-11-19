"""The forecast-file schema and the shape checks it cannot express (PRD §8.3)."""

from __future__ import annotations

import copy
import json
import math
from datetime import timedelta

import pytest
from jsonschema import Draft202012Validator

from lookout_forecast.extract import ExtractOptions, Extractor
from lookout_forecast.fields import resolve_fields
from lookout_forecast.output import DocumentError, encode_document, load_schema, validate_document

from .conftest import RAINIER, RUN


@pytest.fixture
def doc(fake, bucket, storage, manifests, small_grid, data_root) -> dict:
    fake.add_run(RUN, fields=["VIS:surface", "HGT:cloud base", "HGT:surface"], hours=range(49))
    extractor = Extractor(
        storage,
        bucket,
        manifests,
        grid=small_grid,
        cache_root=data_root / "cache",
        clock=lambda: RUN + timedelta(hours=2),
    )
    opts = ExtractOptions(
        fields=tuple(resolve_fields(["VIS", "CLOUDBASE", "TERRAIN"])),
        max_fh=1,
        viewpoint_ids=(RAINIER,),
    )
    return copy.deepcopy(extractor.extract(RUN, opts).documents[RAINIER])


def test_schema_is_valid_draft_2020_12() -> None:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["source"]["const"] == "noaa-hrrr-bdp-pds"


def test_written_document_validates(doc) -> None:
    validate_document(doc)
    assert json.loads(encode_document(doc)) == doc


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["values"].pop(), "field_names"),
        (lambda d: d["values"][0].pop(), "hours"),
        (lambda d: d["values"][0][0].pop(), "points"),
        (lambda d: d["fields"].pop("VIS:surface"), "fields keys"),
        (lambda d: d.update(extra=1), "schema"),
        (lambda d: d.update(run_start_utc="2026-11-19T06:30:00Z"), "schema"),
        (lambda d: d["hours"][0].update(bytes_downloaded=1), "add up"),
        (lambda d: d["hours"][0].update(availability_source="modeled"), "availability_model"),
        (lambda d: d["points"][0].update(i=None), "all set or all null"),
        (lambda d: d["points"][1].update(id="camera"), "duplicate point ids"),
        (lambda d: d.update(forecast_hours=[1, 0]), "hours"),
        (lambda d: d["fields"]["VIS:surface"].update(datum="above ground"), "schema"),
    ],
)
def test_broken_documents_are_rejected(doc, mutate, message) -> None:
    mutate(doc)
    with pytest.raises(DocumentError, match=message):
        validate_document(doc)


def test_nan_is_never_written(doc) -> None:
    doc["values"][0][0][0] = math.nan
    with pytest.raises(ValueError):
        encode_document(doc)
