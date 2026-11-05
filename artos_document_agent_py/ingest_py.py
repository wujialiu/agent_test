"""Import bounded, versioned CSV result tables and JSON protocol records.

A source reference points to a specific document version, content hash, and
row or JSON Pointer. The draft retains the excerpt for later review.
"""

import csv
import io
import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from pydantic import Field, ValidationError

from .domain_py import (
    Evidence,
    FactKey,
    FactValue,
    Record,
    ShortText,
    Slug,
    Snapshot,
    SourceRef,
    fingerprint,
)

MAX_BYTES = 180_000
MAX_FACTS = 300
KEY_FIELDS = ("metric", "population", "arm", "timepoint")
VALUE_FIELDS = ("value", "unit", "denominator")
COLUMNS = set(KEY_FIELDS + VALUE_FIELDS) | {"study"}
FIXTURES = Path(__file__).with_name("fixtures")


class SourceInput(Record):
    document_id: Slug
    version: Slug
    filename: ShortText


class Manifest(Record):
    study: Slug
    label: ShortText
    sources: Annotated[tuple[SourceInput, ...], Field(min_length=1, max_length=8)]


def read_source(root: Path, filename: str) -> bytes:
    # Manifest paths are deliberately limited to regular files in one directory.
    path = root / filename
    if path.name != filename or path.is_symlink() or not path.is_file():
        raise ValueError(f"source must be a regular file in the manifest directory: {filename}")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"source exceeds the byte limit: {filename}")
    return data


def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON contains duplicate keys")
    return result


def json_object(raw: bytes) -> dict:
    data = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def rows(raw: bytes, suffix: str):
    if suffix == ".json":
        data = json_object(raw)
        if set(data) != {"facts"} or not isinstance(data["facts"], list):
            raise ValueError("JSON source must have a facts array")
        for index, fact in enumerate(data["facts"]):
            yield f"/facts/{index}", fact
        return

    if suffix != ".csv":
        raise ValueError("source must be CSV or JSON")
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if set(reader.fieldnames or []) != COLUMNS or len(reader.fieldnames) != len(COLUMNS):
        raise ValueError("CSV headers must match the fact schema")
    previous = reader.line_num
    for row in reader:
        start = previous + 1
        previous = reader.line_num
        yield f"CSV lines {start}–{previous}" if start != previous else f"CSV line {start}", row


def evidence_row(study: str, source: SourceRef, locator: str, row: object) -> Evidence:
    if not isinstance(row, dict) or set(row) != COLUMNS:
        raise ValueError("fact row does not match the required fields")
    if row["study"] != study:
        raise ValueError("fact row belongs to a different study")
    denominator = row["denominator"]
    # Both formats preserve the integer numerator as text. An empty CSV cell
    # denotes the same thing as a JSON null.
    if isinstance(denominator, str) and denominator.isascii() and denominator.isdecimal():
        denominator = int(denominator)
    elif denominator == "":
        denominator = None
    if denominator is not None and type(denominator) is not int:
        raise ValueError("denominator must be a positive integer or empty")
    return Evidence(
        id=fingerprint([study, source.document_id, source.version, source.sha256, locator]),
        source=source,
        locator=locator,
        excerpt=json.dumps(row, ensure_ascii=False, sort_keys=True),
        key=FactKey.model_validate({key: row[key] for key in KEY_FIELDS}),
        fact=FactValue(value=row["value"], unit=row["unit"], denominator=denominator),
    )


def load_manifest(path: Path) -> Snapshot:
    try:
        manifest = Manifest.model_validate(json_object(read_source(path.parent, path.name)))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid manifest: {exc}") from None

    if len({source.document_id for source in manifest.sources}) != len(manifest.sources):
        raise ValueError("each snapshot must select exactly one version of each document")

    sources = []
    evidence = []
    for item in sorted(manifest.sources, key=lambda source: source.document_id):
        try:
            raw = read_source(path.parent, item.filename)
            source = SourceRef(**item.model_dump(), sha256=sha256(raw).hexdigest())
            sources.append(source)
            for locator, row in rows(raw, Path(item.filename).suffix):
                try:
                    evidence.append(evidence_row(manifest.study, source, locator, row))
                except (ValidationError, ValueError) as exc:
                    raise ValueError(f"{locator}: {exc}") from None
                if len(evidence) > MAX_FACTS:
                    raise ValueError("snapshot exceeded the maximum fact count")
        except (OSError, csv.Error, ValueError) as exc:
            raise ValueError(f"invalid source {item.filename}: {exc}") from None

    snapshot_id = fingerprint(
        [manifest.study, manifest.label]
        + [
            part
            for source in sources
            for part in (source.document_id, source.version, source.filename, source.sha256)
        ]
    )
    return Snapshot(
        id=snapshot_id,
        study=manifest.study,
        label=manifest.label,
        sources=tuple(sources),
        evidence=tuple(evidence),
    )


def demo_snapshot(name: str) -> Snapshot:
    if name not in {"baseline", "amended", "conflict"}:
        raise ValueError("choose the baseline, amended, or conflict scenario")
    return load_manifest(FIXTURES / f"{name}.json")
