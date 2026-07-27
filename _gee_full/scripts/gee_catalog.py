#!/usr/bin/env python3
"""Load, validate, filter, rank, compare, and explain GEE catalog records."""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
import lzma
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_ASSETS_DIR = SKILL_DIR / "assets"
CATALOG_FILENAME = "catalog.jsonl.gz"
CATALOG_TEXT_FILENAME = "catalog.jsonl.xz.base64.txt"
CATALOG_SUMMARY_FILENAME = "catalog-summary.tsv"
MANIFEST_FILENAME = "manifest.json"
AUDIT_OVERRIDES_FILENAME = "audit-overrides.json"

CATALOG_SUMMARY_COLUMNS = (
    "asset_id",
    "title_zh_cn",
    "title_en",
    "summary_zh_cn",
    "summary_en",
    "type",
    "status",
    "start_date",
    "end_date",
    "gsd_min_m",
    "gsd_max_m",
    "cadence",
    "provider",
    "bands",
    "categories",
    "keywords",
    "bbox",
    "license",
    "source_url",
)

OPEN_LICENSE_MARKERS = {
    "apache",
    "cc-by",
    "cc0",
    "mit",
    "ogl",
    "open data commons",
    "public-domain",
    "public domain",
    "us-pd",
}

QUERY_EXPANSIONS = {
    "surface reflectance": "sr reflectance satellite imagery sentinel landsat optical",
    "optical": "multispectral reflectance sentinel landsat satellite imagery",
    "radar": "sar synthetic aperture sentinel-1 backscatter",
    "dem": "elevation dsm dtm topography",
    "precipitation": "rainfall climate weather",
    "land cover": "landcover classification",
    "vegetation index": "ndvi evi vegetation",
    "地表反射率": "surface reflectance sr sentinel landsat 光学 卫星影像",
    "光学": "optical multispectral satellite imagery sentinel landsat reflectance",
    "雷达": "radar sar synthetic aperture sentinel-1",
    "合成孔径雷达": "radar sar synthetic aperture sentinel-1",
    "高程": "elevation dem dsm dtm topography",
    "降水": "precipitation rainfall climate weather",
    "气温": "temperature climate weather",
    "土地覆盖": "landcover land cover classification",
    "植被指数": "vegetation index ndvi evi",
    "红边": "red edge red-edge",
    "人口": "population demography worldpop landscan",
    "热岛": "urban heat island land surface temperature lst",
    "pm2.5": "pm2.5 pm25 particulate matter aerosol air quality",
    "空气质量": "air quality pollution no2 aerosol pm25",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_text(value: Any) -> str:
    return clean_text(value).casefold()


def tokenize(value: str) -> list[str]:
    normalized = normalize_text(value)
    latin = re.findall(r"[a-z0-9][a-z0-9_./+-]*", normalized)
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    chinese: list[str] = []
    for run in chinese_runs:
        chinese.append(run)
        if len(run) > 1:
            chinese.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(latin + chinese))


def expand_query(value: str) -> str:
    normalized = normalize_text(value)
    additions = [expanded for phrase, expanded in QUERY_EXPANSIONS.items() if phrase in normalized]
    return clean_text(" ".join([value, *additions]))


def text_contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    if re.fullmatch(r"[a-z0-9]+", needle):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack))
    return needle in haystack


def parse_date(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return text.split("T", 1)[0]


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def is_open_license(value: str) -> bool:
    normalized = normalize_text(value)
    if not normalized or normalized == "proprietary":
        return False
    return any(marker in normalized for marker in OPEN_LICENSE_MARKERS)


def bbox_intersects(left: Sequence[float], right: Sequence[float]) -> bool:
    if len(left) < 4 or len(right) < 4:
        return False
    return not (
        float(left[2]) < float(right[0])
        or float(left[0]) > float(right[2])
        or float(left[3]) < float(right[1])
        or float(left[1]) > float(right[3])
    )


def bbox_contains(container: Sequence[float], target: Sequence[float]) -> bool:
    if len(container) < 4 or len(target) < 4:
        return False
    return (
        float(container[0]) <= float(target[0])
        and float(container[1]) <= float(target[1])
        and float(container[2]) >= float(target[2])
        and float(container[3]) >= float(target[3])
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return _read_jsonl(handle, path)


def read_jsonl_text_transport(path: Path) -> list[dict[str, Any]]:
    content = decode_catalog_text_transport(path)
    with io.TextIOWrapper(io.BytesIO(content), encoding="utf-8") as handle:
        return _read_jsonl(handle, path)


def decode_catalog_text_transport(path: Path) -> bytes:
    try:
        encoded = b"".join(path.read_bytes().split())
        compressed = base64.b64decode(encoded, validate=True)
        return lzma.decompress(compressed)
    except (OSError, ValueError, lzma.LZMAError) as exc:
        raise ValueError(f"Invalid Base64/XZ catalog at {path}: {exc}") from exc


def _read_jsonl(handle: Iterable[str], source: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(handle, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_number} of {source}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Catalog line {line_number} of {source} is not an object")
        records.append(value)
    return records


def write_jsonl_gz(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9, newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def write_catalog_text_transport(source: Path, target: Path) -> str:
    """Write a text-only transport copy for registries that exclude binary files."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(source, "rb") as handle:
        content = handle.read()
    encoded = base64.b64encode(lzma.compress(content, preset=9)).decode("ascii")
    with target.open("w", encoding="ascii", newline="\n") as handle:
        for offset in range(0, len(encoded), 76):
            handle.write(encoded[offset : offset + 76])
            handle.write("\n")
    return hashlib.sha256(content).hexdigest()


def write_catalog_summary(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Write a bilingual, spreadsheet-friendly human-readable catalog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CATALOG_SUMMARY_COLUMNS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for record in sorted(records, key=lambda item: clean_text(item.get("id")).casefold()):
            localizations = record.get("localizations") or {}
            localized_en = localizations.get("en") or {}
            localized_zh = localizations.get("zh-cn") or {}
            cadence = record.get("cadence") or {}
            cadence_text = clean_text(
                " ".join(
                    str(value)
                    for value in (cadence.get("interval"), cadence.get("unit"), cadence.get("type"))
                    if value not in (None, "")
                )
            )
            writer.writerow(
                {
                    "asset_id": clean_text(record.get("id")),
                    "title_zh_cn": clean_text(localized_zh.get("title")),
                    "title_en": clean_text(localized_en.get("title") or record.get("title")),
                    "summary_zh_cn": clean_text(localized_zh.get("summary")),
                    "summary_en": clean_text(
                        localized_en.get("summary") or record.get("description")
                    ),
                    "type": clean_text(record.get("gee_type")),
                    "status": clean_text(record.get("status")),
                    "start_date": clean_text(record.get("start_date")),
                    "end_date": clean_text(record.get("end_date")),
                    "gsd_min_m": record.get("gsd_min") if record.get("gsd_min") is not None else "",
                    "gsd_max_m": record.get("gsd_max") if record.get("gsd_max") is not None else "",
                    "cadence": cadence_text,
                    "provider": clean_text(record.get("primary_provider")),
                    "bands": ", ".join(
                        clean_text(band.get("name"))
                        for band in record.get("bands", [])
                        if clean_text(band.get("name"))
                    ),
                    "categories": ", ".join(map(clean_text, record.get("categories", []))),
                    "keywords": ", ".join(map(clean_text, record.get("keywords", []))),
                    "bbox": ", ".join(map(str, record.get("bbox", []))),
                    "license": clean_text(record.get("license")),
                    "source_url": clean_text(record.get("source_url")),
                }
            )


@dataclass
class SearchOptions:
    query: str = ""
    dataset_types: set[str] = field(default_factory=set)
    provider: str = ""
    tags: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    bands: set[str] = field(default_factory=set)
    band_text: str = ""
    max_band_gsd: float | None = None
    status: str = "ready"
    license_text: str = ""
    open_license: bool = False
    min_gsd: float | None = None
    max_gsd: float | None = None
    temporal_start: str = ""
    temporal_end: str = ""
    bbox: list[float] | None = None
    require_full_coverage: bool = False
    require_full_temporal: bool = False
    exclude_suspect_bbox: bool = True
    language: str = "en"
    limit: int = 10


def _localized(record: dict[str, Any], language: str) -> dict[str, Any]:
    localizations = record.get("localizations") or {}
    if language in localizations:
        return localizations[language] or {}
    normalized = language.lower().replace("_", "-")
    return localizations.get(normalized) or localizations.get("en") or {}


def display_value(record: dict[str, Any], key: str, language: str) -> Any:
    localized = _localized(record, language)
    value = localized.get(key)
    if value not in (None, "", [], {}):
        return value
    return record.get(key)


def gee_snippet(record: dict[str, Any]) -> str:
    asset_id = record.get("id", "")
    dataset_type = record.get("gee_type", "")
    constructor = {
        "image": "ee.Image",
        "image_collection": "ee.ImageCollection",
        "table": "ee.FeatureCollection",
        "table_collection": "ee.FeatureCollection",
        "bigquery_table": "ee.FeatureCollection",
    }.get(dataset_type, "ee.ImageCollection")
    return f'{constructor}("{asset_id}")'


def validate_records(records: Sequence[dict[str, Any]], minimum_count: int = 500) -> dict[str, Any]:
    required = ("id", "slug", "title", "gee_type", "status", "source_url")
    errors: list[str] = []
    warnings: list[str] = []
    ids: set[str] = set()
    slugs: set[str] = set()
    missing_counts = {key: 0 for key in required}

    if len(records) < minimum_count:
        errors.append(f"Catalog has {len(records)} records; expected at least {minimum_count}")

    for index, record in enumerate(records):
        for key in required:
            if record.get(key) in (None, "", [], {}):
                missing_counts[key] += 1
                if len(errors) < 100:
                    errors.append(f"Record {index} is missing {key}")
        asset_id = clean_text(record.get("id"))
        slug = clean_text(record.get("slug"))
        if asset_id in ids:
            errors.append(f"Duplicate asset id: {asset_id}")
        if slug in slugs:
            errors.append(f"Duplicate catalog slug: {slug}")
        ids.add(asset_id)
        slugs.add(slug)

        bbox = record.get("bbox") or []
        if bbox and (len(bbox) != 4 or any(number(item) is None for item in bbox)):
            errors.append(f"Invalid bbox for {asset_id}: {bbox}")
        if record.get("gsd_min") is not None and record.get("gsd_max") is not None:
            if float(record["gsd_min"]) > float(record["gsd_max"]):
                errors.append(f"Invalid GSD range for {asset_id}")

    statuses: dict[str, int] = {}
    types: dict[str, int] = {}
    for record in records:
        statuses[clean_text(record.get("status")) or "unknown"] = statuses.get(clean_text(record.get("status")) or "unknown", 0) + 1
        types[clean_text(record.get("gee_type")) or "unknown"] = types.get(clean_text(record.get("gee_type")) or "unknown", 0) + 1

    ready_count = statuses.get("ready", 0)
    if records and ready_count / len(records) < 0.5:
        warnings.append("Less than half of catalog records have status=ready")

    return {
        "valid": not errors,
        "record_count": len(records),
        "errors": errors,
        "warnings": warnings,
        "missing_required_fields": missing_counts,
        "status_counts": dict(sorted(statuses.items())),
        "type_counts": dict(sorted(types.items())),
    }


class Catalog:
    def __init__(self, records: Sequence[dict[str, Any]], manifest: dict[str, Any] | None = None):
        self.records = list(records)
        self.manifest = manifest or {}
        self.by_id = {normalize_text(record.get("id")): record for record in self.records}
        self.by_slug = {normalize_text(record.get("slug")): record for record in self.records}

    @classmethod
    def load(cls, assets_dir: Path | str | None = None) -> "Catalog":
        directory = Path(assets_dir) if assets_dir else DEFAULT_ASSETS_DIR
        catalog_path = directory / CATALOG_FILENAME
        text_catalog_path = directory / CATALOG_TEXT_FILENAME
        manifest_path = directory / MANIFEST_FILENAME
        if catalog_path.exists():
            records = read_jsonl_gz(catalog_path)
        elif text_catalog_path.exists():
            records = read_jsonl_text_transport(text_catalog_path)
        else:
            raise FileNotFoundError(
                f"Catalog data not found at {catalog_path} or {text_catalog_path}. "
                "Run update_catalog.py first."
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        audit_path = directory / AUDIT_OVERRIDES_FILENAME
        if audit_path.exists():
            audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
            for record in records:
                override = audit_data.get("records", {}).get(record.get("id"))
                if override:
                    record["audit"] = override
        return cls(records, manifest)

    def get(self, asset_id_or_slug: str) -> dict[str, Any] | None:
        key = normalize_text(asset_id_or_slug)
        return self.by_id.get(key) or self.by_slug.get(key)

    def _matches_filters(self, record: dict[str, Any], options: SearchOptions) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        audit = record.get("audit") or {}
        if (
            options.exclude_suspect_bbox
            and options.require_full_coverage
            and audit.get("bbox_quality") in {"confirmed_suspicious", "uncertain", "invalid"}
        ):
            return False, []
        if options.status and options.status != "all":
            if normalize_text(record.get("status")) != normalize_text(options.status):
                return False, []
            reasons.append(f"status={record.get('status')}")

        if options.dataset_types and normalize_text(record.get("gee_type")) not in options.dataset_types:
            return False, []
        if options.dataset_types:
            reasons.append(f"type={record.get('gee_type')}")

        providers = " ".join(provider.get("name", "") for provider in record.get("providers", []))
        if options.provider and normalize_text(options.provider) not in normalize_text(providers):
            return False, []
        if options.provider:
            reasons.append(f"provider matches {options.provider}")

        keywords = {normalize_text(item) for item in record.get("keywords", [])}
        if options.tags and not options.tags.issubset(keywords):
            return False, []
        if options.tags:
            reasons.append("tags=" + ",".join(sorted(options.tags)))

        categories = {normalize_text(item) for item in record.get("categories", [])}
        if options.categories and not options.categories.intersection(categories):
            return False, []
        if options.categories:
            reasons.append("category match")

        band_names = {normalize_text(item.get("name")) for item in record.get("bands", [])}
        if options.bands and not options.bands.issubset(band_names):
            return False, []
        if options.bands:
            reasons.append("bands=" + ",".join(sorted(options.bands)))

        matching_bands = list(record.get("bands", []))
        if options.band_text:
            band_terms = tokenize(options.band_text)
            matching_bands = [
                band
                for band in matching_bands
                if all(
                    text_contains(
                        normalize_text(" ".join([clean_text(band.get("name")), clean_text(band.get("description"))])),
                        term,
                    )
                    for term in band_terms
                )
            ]
            if not matching_bands:
                return False, []
            reasons.append(f"band metadata matches {options.band_text}")
        elif options.bands:
            matching_bands = [
                band for band in matching_bands if normalize_text(band.get("name")) in options.bands
            ]
        if options.max_band_gsd is not None:
            matching_band_gsds = [number(band.get("gsd")) for band in matching_bands]
            if not matching_band_gsds or any(
                gsd is None or gsd > options.max_band_gsd for gsd in matching_band_gsds
            ):
                return False, []
            reasons.append(f"matched band resolution <= {options.max_band_gsd:g}m")

        license_value = clean_text(record.get("license"))
        if options.license_text and normalize_text(options.license_text) not in normalize_text(license_value):
            return False, []
        if options.open_license and not bool(record.get("open_license")):
            return False, []
        if options.license_text or options.open_license:
            reasons.append(f"license={license_value or 'unspecified'}")

        gsd_min = number(record.get("gsd_min"))
        gsd_max = number(record.get("gsd_max"))
        if options.max_gsd is not None and (gsd_min is None or gsd_min > options.max_gsd):
            return False, []
        if options.min_gsd is not None and (gsd_max is None or gsd_max < options.min_gsd):
            return False, []
        if options.max_gsd is not None or options.min_gsd is not None:
            reasons.append(f"resolution={gsd_min:g}-{gsd_max:g}m" if gsd_min is not None and gsd_max is not None else "resolution match")

        start = parse_date(record.get("start_date"))
        end = parse_date(record.get("end_date")) or "9999-12-31"
        requested_start = parse_date(options.temporal_start)
        requested_end = parse_date(options.temporal_end)
        if options.require_full_temporal:
            if requested_start and (not start or start > requested_start):
                return False, []
            if requested_end and end < requested_end:
                return False, []
        else:
            if requested_start and end < requested_start:
                return False, []
            if requested_end and start and start > requested_end:
                return False, []
        if options.temporal_start or options.temporal_end:
            qualifier = "fully covers" if options.require_full_temporal else "overlaps"
            reasons.append(f"time {qualifier}: {start or '?'} to {record.get('end_date') or 'present'}")

        if options.bbox:
            record_bbox = record.get("bbox") or []
            spatial_match = (
                bbox_contains(record_bbox, options.bbox)
                if options.require_full_coverage
                else bbox_intersects(record_bbox, options.bbox)
            )
            if not record_bbox or not spatial_match:
                return False, []
            reasons.append(
                "spatial extent fully covers requested bbox"
                if options.require_full_coverage
                else "spatial extent intersects requested bbox"
            )

        return True, reasons

    def _text_score(self, record: dict[str, Any], query: str, language: str) -> tuple[float, list[str]]:
        if not clean_text(query):
            return 1.0, []
        expanded_query = expand_query(query)
        terms = tokenize(expanded_query)
        if not terms:
            return 1.0, []

        localized = _localized(record, language)
        fields = [
            (record.get("id", ""), 12.0, "asset id"),
            (record.get("slug", ""), 10.0, "catalog id"),
            (display_value(record, "title", language), 8.0, "title"),
            (" ".join(record.get("keywords", [])), 6.0, "tags"),
            (" ".join(record.get("categories", [])), 5.0, "category"),
            (" ".join(item.get("name", "") for item in record.get("bands", [])), 5.0, "bands"),
            (" ".join(provider.get("name", "") for provider in record.get("providers", [])), 4.0, "provider"),
            (f"{record.get('gsd_min')}m {record.get('gsd_max')}m", 4.0, "resolution"),
            (localized.get("summary", ""), 3.0, "localized summary"),
            (record.get("description", ""), 2.0, "description"),
        ]
        score = 0.0
        matched: list[str] = []
        matched_terms: set[str] = set()
        phrase = normalize_text(query)
        for value, weight, label in fields:
            haystack = normalize_text(value)
            if not haystack:
                continue
            hits = {term for term in terms if text_contains(haystack, term)}
            field_hits = len(hits)
            if field_hits:
                score += weight * field_hits / len(terms)
                matched.append(label)
                matched_terms.update(hits)
            if phrase and text_contains(haystack, phrase):
                score += weight * 1.5
        if terms:
            score += 8.0 * len(matched_terms) / len(terms)
        return score, list(dict.fromkeys(matched))

    def search(self, options: SearchOptions) -> list[dict[str, Any]]:
        started = time.perf_counter()
        ranked: list[tuple[float, dict[str, Any], list[str]]] = []
        normalized_types = {normalize_text(item) for item in options.dataset_types}
        normalized_tags = {normalize_text(item) for item in options.tags}
        normalized_categories = {normalize_text(item) for item in options.categories}
        normalized_bands = {normalize_text(item) for item in options.bands}
        options = SearchOptions(
            **{
                **options.__dict__,
                "dataset_types": normalized_types,
                "tags": normalized_tags,
                "categories": normalized_categories,
                "bands": normalized_bands,
            }
        )

        for record in self.records:
            matches, filter_reasons = self._matches_filters(record, options)
            if not matches:
                continue
            score, text_reasons = self._text_score(record, options.query, options.language)
            if options.query and score <= 0:
                continue
            if record.get("status") == "ready":
                score += 0.25
            if record.get("source_url"):
                score += 0.05
            ranked.append((score, record, filter_reasons + (["text matches " + ", ".join(text_reasons)] if text_reasons else [])))

        ranked.sort(key=lambda item: (-item[0], normalize_text(item[1].get("title")), normalize_text(item[1].get("id"))))
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        results: list[dict[str, Any]] = []
        for score, record, reasons in ranked[: max(1, options.limit)]:
            results.append(self.explain(record, options.language, score=round(score, 4), reasons=reasons, query_ms=elapsed_ms))
        return results

    def explain(
        self,
        record: dict[str, Any],
        language: str = "en",
        *,
        score: float | None = None,
        reasons: Sequence[str] | None = None,
        query_ms: float | None = None,
        full: bool = False,
    ) -> dict[str, Any]:
        band_names = [band.get("name") for band in record.get("bands", []) if band.get("name")]
        band_limit = 64
        result = {
            "id": record.get("id"),
            "slug": record.get("slug"),
            "title": display_value(record, "title", language),
            "summary": display_value(record, "summary", language) or record.get("description"),
            "type": record.get("gee_type"),
            "status": record.get("status"),
            "provider": record.get("primary_provider"),
            "time": {"start": record.get("start_date"), "end": record.get("end_date")},
            "cadence": record.get("cadence") or {},
            "bbox": record.get("bbox"),
            "gsd_m": {"min": record.get("gsd_min"), "max": record.get("gsd_max")},
            "band_count": len(band_names),
            "bands": band_names[:band_limit],
            "bands_truncated": len(band_names) > band_limit,
            "band_gsd_m": {
                band.get("name"): number(band.get("gsd"))
                for band in record.get("bands", [])[:band_limit]
                if band.get("name") and number(band.get("gsd")) is not None
            },
            "categories": record.get("categories", []),
            "tags": record.get("keywords", []),
            "license": record.get("license"),
            "open_license": bool(record.get("open_license")),
            "citation": record.get("citation"),
            "doi": record.get("doi"),
            "gee_snippet": gee_snippet(record),
            "source_url": record.get("source_url"),
            "stac_url": record.get("stac_url"),
            "translation_source": (_localized(record, language) or {}).get("translation_source", "original"),
            "bbox_audit": record.get("audit") or {"bbox_quality": "unreviewed"},
        }
        if score is not None:
            result["score"] = score
        if reasons:
            result["why"] = list(dict.fromkeys(reasons))
        if query_ms is not None:
            result["query_ms"] = query_ms
        if full:
            result["band_details"] = record.get("bands", [])
            localized = _localized(record, language)
            if localized.get("detail"):
                result["localized_detail"] = localized["detail"]
        return result

    def compare(self, asset_ids: Sequence[str], language: str = "en") -> dict[str, Any]:
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        for asset_id in asset_ids:
            record = self.get(asset_id)
            if record is None:
                missing.append(asset_id)
            else:
                found.append(self.explain(record, language))
        return {"datasets": found, "missing": missing, "count": len(found)}

    def stats(self) -> dict[str, Any]:
        validation = validate_records(self.records)
        return {
            "generated_at": self.manifest.get("generated_at"),
            "record_count": len(self.records),
            "status_counts": validation["status_counts"],
            "type_counts": validation["type_counts"],
            "providers": len({record.get("primary_provider") for record in self.records if record.get("primary_provider")}),
            "categories": len({item for record in self.records for item in record.get("categories", [])}),
            "tags": len({item for record in self.records for item in record.get("keywords", [])}),
            "languages": self.manifest.get("languages", []),
        }


def markdown_results(results: Sequence[dict[str, Any]]) -> str:
    if not results:
        return "No matching Earth Engine datasets were found."
    lines: list[str] = []
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. **{item.get('title')}** (`{item.get('id')}`)")
        lines.append(f"   - Type/status: `{item.get('type')}` / `{item.get('status')}`")
        lines.append(f"   - Resolution: {item.get('gsd_m', {}).get('min')} to {item.get('gsd_m', {}).get('max')} m")
        lines.append(f"   - Time: {item.get('time', {}).get('start') or '?'} to {item.get('time', {}).get('end') or 'present'}")
        if item.get("why"):
            lines.append("   - Why: " + "; ".join(item["why"]))
        lines.append(f"   - Code: `{item.get('gee_snippet')}`")
        lines.append(f"   - Source: {item.get('source_url')}")
    return "\n".join(lines)


def iter_catalog_lines(path: Path) -> Iterator[dict[str, Any]]:
    yield from read_jsonl_gz(path)
