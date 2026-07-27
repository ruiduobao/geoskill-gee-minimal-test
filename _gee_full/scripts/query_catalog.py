#!/usr/bin/env python3
"""Query, inspect, compare, or summarize the bundled GEE dataset catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gee_catalog import Catalog, DEFAULT_ASSETS_DIR, SearchOptions, markdown_results

# Path setup so the vendored _geoskill_core.aoi / place_resolver shim is importable.
_HERE = Path(__file__).resolve().parent
_SKILL_DIR = _HERE.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))
try:
    from place_resolver import resolve_place as _resolve_place  # type: ignore
    from place_resolver import PlaceNotFoundError as _PlaceNotFoundError  # type: ignore
    _HAS_PLACE_RESOLVER = True
except Exception:  # noqa: BLE001
    _resolve_place = None
    _PlaceNotFoundError = Exception
    _HAS_PLACE_RESOLVER = False


REGION_BBOXES = {
    "africa": [-18.0, -35.0, 52.0, 38.0],
    "asia": [25.0, -11.0, 180.0, 81.0],
    "china": [73.0, 18.0, 135.0, 54.0],
    "europe": [-25.0, 34.0, 45.0, 72.0],
    "global": [-180.0, -90.0, 180.0, 90.0],
    "north-america": [-170.0, 5.0, -50.0, 84.0],
    "south-america": [-82.0, -56.0, -34.0, 13.0],
    "usa": [-125.0, 24.0, -66.0, 50.0],
}


def csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def parse_bbox(value: str) -> list[float]:
    parts = [item.strip() for item in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be min_lon,min_lat,max_lon,max_lat")
    try:
        bbox = [float(item) for item in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from exc
    if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
        raise argparse.ArgumentTypeError("bbox minimum values must not exceed maximum values")
    return bbox


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--id", help="Return one dataset by asset id or catalog slug")
    action.add_argument("--compare", nargs="+", metavar="ID", help="Compare two or more asset ids")
    action.add_argument("--stats", action="store_true", help="Show catalog summary statistics")

    parser.add_argument("--query", default="", help="Free-text query in English or Chinese")
    parser.add_argument("--type", dest="dataset_types", default="", help="Comma-separated GEE types")
    parser.add_argument("--provider", default="")
    parser.add_argument("--tag", dest="tags", default="", help="Comma-separated required tags")
    parser.add_argument("--category", dest="categories", default="", help="Comma-separated categories; any may match")
    parser.add_argument("--band", dest="bands", default="", help="Comma-separated required band names")
    parser.add_argument("--band-text", default="", help="Required text in a band name or description")
    parser.add_argument("--max-band-gsd", type=float, help="Maximum GSD for every matched/required band")
    parser.add_argument("--status", default="ready", help="Dataset status, or 'all'")
    parser.add_argument("--license", dest="license_text", default="")
    parser.add_argument("--open-license", action="store_true")
    parser.add_argument("--min-gsd", type=float)
    parser.add_argument("--max-gsd", type=float)
    parser.add_argument("--temporal-start", default="", help="Required overlap beginning, YYYY-MM-DD")
    parser.add_argument("--temporal-end", default="", help="Required overlap ending, YYYY-MM-DD")
    parser.add_argument("--require-full-temporal", action="store_true", help="Require coverage of the whole requested period")
    parser.add_argument("--bbox", type=parse_bbox)
    parser.add_argument("--region", choices=tuple(sorted(REGION_BBOXES)), help="Named bbox shortcut")
    parser.add_argument(
        "--place",
        help="Place name (e.g. '北京市', '长江三角洲'). Resolved via "
             "place_resolver (offline hardcoded + Open-Meteo + Nominatim) "
             "and converted to --bbox. Conflicts with --bbox.",
    )
    parser.add_argument("--require-full-coverage", action="store_true", help="Require the dataset bbox to contain the requested region")
    parser.add_argument("--include-suspect-bbox", action="store_true", help="Include records with suspicious or uncertain bbox audits when using full coverage")
    parser.add_argument("--language", choices=("en", "zh-cn"), default="en")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--full", action="store_true", help="Include localized detail tables and band metadata for --id")
    parser.add_argument("--qa", default=None, metavar="PATH",
                        help="Write a JSON run-summary sidecar to PATH (e.g. --qa run.qa.json). "
                             "Records the action (search/id/compare/stats), filters, and "
                             "the resulting record ids / count so each query is auditable.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # Phase 5: --qa sidecar summary. We capture inputs + the resulting
    # record count / ids so each query is auditable. This is intentionally
    # written *before* the output is printed so it works even on errors.
    qa_summary: dict | None = None
    try:
        # --place / --bbox mutual exclusion + resolution
        place_bbox: list[float] | None = None
        if args.place:
            if not _HAS_PLACE_RESOLVER or _resolve_place is None:
                print(json.dumps(
                    {"success": False, "error": "--place requires place_resolver.py"},
                    ensure_ascii=False, indent=2,
                ))
                return 1
            if args.bbox is not None:
                print(json.dumps(
                    {"success": False,
                     "error": "pass either --place or --bbox, not both"},
                    ensure_ascii=False, indent=2,
                ))
                return 1
            try:
                w, s, e, n = _resolve_place(args.place, buffer_deg=0.0)
            except _PlaceNotFoundError as exc:
                print(json.dumps(
                    {"success": False, "error": f"Place resolution failed: {exc}"},
                    ensure_ascii=False, indent=2,
                ))
                return 1
            place_bbox = [float(w), float(s), float(e), float(n)]
        catalog = Catalog.load(args.assets_dir)
        if args.stats:
            output = catalog.stats()
        elif args.id:
            record = catalog.get(args.id)
            if record is None:
                print(json.dumps({"success": False, "error": f"Dataset not found: {args.id}"}, ensure_ascii=False, indent=2))
                return 2
            output = catalog.explain(record, args.language, full=args.full)
        elif args.compare:
            output = catalog.compare(args.compare, args.language)
        else:
            if args.limit < 1 or args.limit > 100:
                raise ValueError("limit must be between 1 and 100")
            options = SearchOptions(
                query=args.query,
                dataset_types=csv_set(args.dataset_types),
                provider=args.provider,
                tags=csv_set(args.tags),
                categories=csv_set(args.categories),
                bands=csv_set(args.bands),
                band_text=args.band_text,
                max_band_gsd=args.max_band_gsd,
                status=args.status,
                license_text=args.license_text,
                open_license=args.open_license,
                min_gsd=args.min_gsd,
                max_gsd=args.max_gsd,
                temporal_start=args.temporal_start,
                temporal_end=args.temporal_end,
                bbox=place_bbox or args.bbox or (REGION_BBOXES.get(args.region) if args.region else None),
                require_full_coverage=args.require_full_coverage,
                require_full_temporal=args.require_full_temporal,
                exclude_suspect_bbox=not args.include_suspect_bbox,
                language=args.language,
                limit=args.limit,
            )
            output = catalog.search(options)
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    if args.format == "markdown" and isinstance(output, list):
        print(markdown_results(output))
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))

    # Phase 5: write --qa sidecar if requested
    if args.qa:
        try:
            qa_summary = _build_qa_summary(args, output)
            qa_p = Path(args.qa)
            qa_p.parent.mkdir(parents=True, exist_ok=True)
            qa_p.write_text(json.dumps(qa_summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as exc:
            print(f"WARN: --qa sidecar could not be written: {exc}", file=sys.stderr)

    return 0


def _build_qa_summary(args, output) -> dict:
    """Build the JSON run-summary dict (Phase 5 helper)."""
    from datetime import datetime as _dt, timezone as _tz
    # Determine which action was used.
    if args.stats:
        action = "stats"
        n_records = (output.get("record_count") if isinstance(output, dict) else None)
        record_ids: list[str] = []
    elif args.id:
        action = "id"
        n_records = 1 if output else 0
        record_ids = [args.id] if output else []
    elif args.compare:
        action = "compare"
        record_ids = list(args.compare)
        n_records = len(record_ids)
    else:
        action = "search"
        if isinstance(output, list):
            record_ids = [r.get("id") for r in output if isinstance(r, dict) and r.get("id")]
            n_records = len(record_ids)
        else:
            record_ids = []
            n_records = 0
    return {
        "skill": "gee-dataset-intelligence",
        "command": action,
        "version": "0.2.0",
        "timestamp": _dt.now(_tz.utc).isoformat(),
        "query": args.query or None,
        "region": args.region,
        "bbox": place_bbox if (place_bbox := _resolved_place_bbox(args)) else None,
        "place": args.place,
        "categories": [c for c in (args.categories or "").split(",") if c],
        "tags": [t for t in (args.tags or "").split(",") if t],
        "bands": [b for b in (args.bands or "").split(",") if b],
        "provider": args.provider or None,
        "min_gsd": args.min_gsd,
        "max_gsd": args.max_gsd,
        "temporal_start": args.temporal_start or None,
        "temporal_end": args.temporal_end or None,
        "limit": args.limit,
        "format": args.format,
        "record_count": n_records,
        "record_ids": record_ids,
    }


def _resolved_place_bbox(args) -> list[float] | None:
    """Return the bbox resolved from --place / --region / --bbox for QA."""
    if args.bbox is not None:
        return list(args.bbox)
    if args.region:
        return list(REGION_BBOXES.get(args.region, [])) or None
    return None


if __name__ == "__main__":
    sys.exit(main())
