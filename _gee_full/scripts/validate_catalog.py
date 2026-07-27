#!/usr/bin/env python3
"""Validate bundled catalog integrity, hashes, fields, and source reconciliation."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path

from gee_catalog import (
    AUDIT_OVERRIDES_FILENAME,
    CATALOG_FILENAME,
    CATALOG_SUMMARY_COLUMNS,
    CATALOG_SUMMARY_FILENAME,
    CATALOG_TEXT_FILENAME,
    DEFAULT_ASSETS_DIR,
    MANIFEST_FILENAME,
    decode_catalog_text_transport,
    read_jsonl_gz,
    read_jsonl_text_transport,
    sha256_file,
    validate_records,
)


def validate_catalog(assets_dir: Path = DEFAULT_ASSETS_DIR) -> dict:
    catalog_path = assets_dir / CATALOG_FILENAME
    text_catalog_path = assets_dir / CATALOG_TEXT_FILENAME
    summary_path = assets_dir / CATALOG_SUMMARY_FILENAME
    manifest_path = assets_dir / MANIFEST_FILENAME
    errors: list[str] = []
    if not catalog_path.exists() and not text_catalog_path.exists():
        return {
            "valid": False,
            "errors": [f"Missing {catalog_path} and {text_catalog_path}"],
        }
    if not manifest_path.exists():
        return {"valid": False, "errors": [f"Missing {manifest_path}"]}

    if catalog_path.exists():
        records = read_jsonl_gz(catalog_path)
        catalog_file_hash = sha256_file(catalog_path)
        with gzip.open(catalog_path, "rb") as handle:
            content_hash = hashlib.sha256(handle.read()).hexdigest()
    else:
        records = read_jsonl_text_transport(text_catalog_path)
        catalog_file_hash = None
        content_hash = hashlib.sha256(
            decode_catalog_text_transport(text_catalog_path)
        ).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = validate_records(records)
    errors.extend(validation["errors"])
    known_ids = {record.get("id") for record in records}
    if manifest.get("record_count") != len(records):
        errors.append(
            f"Manifest record_count={manifest.get('record_count')} but catalog has {len(records)}"
        )
    if catalog_file_hash and manifest.get("catalog_sha256") != catalog_file_hash:
        errors.append("Catalog SHA-256 does not match manifest")
    if manifest.get("catalog_content_sha256") != content_hash:
        errors.append("Catalog content SHA-256 does not match manifest")
    if text_catalog_path.exists():
        decoded_hash = hashlib.sha256(
            decode_catalog_text_transport(text_catalog_path)
        ).hexdigest()
        if decoded_hash != content_hash:
            errors.append("Text transport catalog does not match catalog content")
        expected_text_hash = manifest.get("catalog_text_sha256")
        if expected_text_hash and expected_text_hash != sha256_file(text_catalog_path):
            errors.append("Text transport SHA-256 does not match manifest")
    if not summary_path.exists():
        errors.append(f"Missing {summary_path}")
    else:
        try:
            with summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                summary_rows = list(reader)
                if tuple(reader.fieldnames or ()) != CATALOG_SUMMARY_COLUMNS:
                    errors.append("Catalog summary columns do not match the expected schema")
            summary_ids = {row.get("asset_id") for row in summary_rows}
            if len(summary_rows) != len(records):
                errors.append(
                    f"Catalog summary has {len(summary_rows)} rows; expected {len(records)}"
                )
            if summary_ids != known_ids:
                errors.append("Catalog summary asset IDs do not match the catalog")
            if manifest.get("catalog_summary_sha256") != sha256_file(summary_path):
                errors.append("Catalog summary SHA-256 does not match manifest")
        except (OSError, csv.Error, UnicodeError) as exc:
            errors.append(f"Cannot read catalog summary: {exc}")
    if manifest.get("localized_list_counts", {}).get("en", 0) < 500:
        errors.append("English localized catalog count is unexpectedly low")
    if manifest.get("localized_list_counts", {}).get("zh-cn", 0) < 500:
        errors.append("Chinese localized catalog count is unexpectedly low")
    audit_path = assets_dir / AUDIT_OVERRIDES_FILENAME
    curation_path = assets_dir / "audit-curation.json"
    allowed = {"unreviewed", "likely_valid", "confirmed_suspicious", "uncertain", "invalid"}
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        for asset_id, override in audit.get("records", {}).items():
            if asset_id not in known_ids:
                errors.append(f"Audit override references unknown asset: {asset_id}")
            if override.get("bbox_quality", "unreviewed") not in allowed:
                errors.append(f"Invalid audit bbox_quality for {asset_id}")
    if curation_path.exists():
        curation = json.loads(curation_path.read_text(encoding="utf-8"))
        for asset_id, decision in curation.get("records", {}).items():
            if asset_id not in known_ids:
                errors.append(f"Audit curation references unknown asset: {asset_id}")
            if decision.get("bbox_quality") not in allowed:
                errors.append(f"Invalid curated bbox_quality for {asset_id}")
            if not str(decision.get("reason", "")).strip():
                errors.append(f"Audit curation has no reason for {asset_id}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": validation["warnings"],
        "record_count": len(records),
        "status_counts": validation["status_counts"],
        "type_counts": validation["type_counts"],
        "manifest_generated_at": manifest.get("generated_at"),
        "audit_override_count": len(json.loads(audit_path.read_text(encoding="utf-8")).get("records", {})) if audit_path.exists() else 0,
        "audit_curation_count": len(json.loads(curation_path.read_text(encoding="utf-8")).get("records", {})) if curation_path.exists() else 0,
        "source_differences": {
            key: len(value)
            for key, value in manifest.get("differences", {}).items()
            if isinstance(value, list)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    args = parser.parse_args()
    result = validate_catalog(args.assets_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
