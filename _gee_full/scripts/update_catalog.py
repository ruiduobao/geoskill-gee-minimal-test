#!/usr/bin/env python3
"""Refresh the complete GEE catalog with one importable function or CLI call."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from gee_catalog import (
    CATALOG_FILENAME,
    CATALOG_SUMMARY_FILENAME,
    CATALOG_TEXT_FILENAME,
    DEFAULT_ASSETS_DIR,
    MANIFEST_FILENAME,
    clean_text,
    is_open_license,
    number,
    parse_date,
    read_jsonl_gz,
    sha256_file,
    utc_now,
    validate_records,
    write_catalog_summary,
    write_catalog_text_transport,
    write_jsonl_gz,
)


STAC_ROOT = "https://storage.googleapis.com/earthengine-stac/catalog/catalog.json"
CATALOG_URL = "https://developers.google.com/earth-engine/datasets/catalog"
RESERVED_CATALOG_PATHS = {"landsat", "modis", "sentinel", "release-notes"}
USER_AGENT = "gee-dataset-intelligence/1.0 (+https://github.com/ruiduobao/gee-dataset-intelligence-skill)"


class FetchError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, proxy: str | None = None, timeout: float = 40.0, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            handler = urllib.request.ProxyHandler()
        self.opener = urllib.request.build_opener(handler)

    def fetch(self, url: str) -> tuple[bytes, dict[str, str]]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en,zh-CN;q=0.9",
                },
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    return response.read(), headers
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(min(8.0, (2**attempt) + random.random()))
        raise FetchError(f"Failed to fetch {url}: {last_error}")

    def fetch_json(self, url: str) -> dict[str, Any]:
        payload, _ = self.fetch(url)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(f"Invalid JSON from {url}: {exc}") from exc
        if not isinstance(value, dict):
            raise FetchError(f"Expected a JSON object from {url}")
        return value

    def fetch_text(self, url: str) -> str:
        payload, headers = self.fetch(url)
        content_type = headers.get("content-type", "")
        match = re.search(r"charset=([^;]+)", content_type, re.I)
        encoding = match.group(1).strip() if match else "utf-8"
        return payload.decode(encoding, errors="replace")


def _catalog_slug(href: str) -> str:
    path = urllib.parse.urlparse(href).path.rstrip("/")
    marker = "/earth-engine/datasets/catalog/"
    if marker not in path:
        return ""
    slug = urllib.parse.unquote(path.split(marker, 1)[1]).strip("/")
    if not slug or "/" in slug or slug.casefold() in RESERVED_CATALOG_PATHS:
        return ""
    return slug


class CatalogListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.current: dict[str, Any] | None = None
        self.in_row = False
        self.row_parts: list[str] = []
        self.in_title = False
        self.records: dict[str, dict[str, Any]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self.current = {"slug": "", "url": "", "title_parts": [], "rows": [], "tags": []}
            return
        if self.current is None:
            return
        if tag == "tr":
            self.in_row = True
            self.row_parts = []
        elif tag == "h3":
            self.in_title = True
        elif tag == "a":
            href = values.get("href") or ""
            slug = _catalog_slug(href)
            if slug and not self.current["slug"]:
                self.current["slug"] = slug
                self.current["url"] = urllib.parse.urljoin(CATALOG_URL, href)
            tag_marker = "/earth-engine/datasets/tags/"
            path = urllib.parse.urlparse(href).path
            if tag_marker in path:
                tag_value = urllib.parse.unquote(path.split(tag_marker, 1)[1]).strip("/")
                if tag_value:
                    self.current["tags"].append(tag_value)

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        if self.in_row:
            self.row_parts.append(data)
        if self.in_title:
            self.current["title_parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is not None:
            if tag == "tr" and self.in_row:
                row = clean_text(" ".join(self.row_parts))
                if row:
                    self.current["rows"].append(row)
                self.in_row = False
                self.row_parts = []
            elif tag == "h3":
                self.in_title = False
        if tag == "table" and self.table_depth:
            self.table_depth -= 1
            if self.table_depth == 0 and self.current is not None:
                slug = self.current["slug"]
                if slug:
                    title = clean_text(" ".join(self.current["title_parts"]))
                    rows = self.current["rows"]
                    summary = rows[1] if len(rows) > 1 else ""
                    self.records[slug] = {
                        "title": title or (rows[0] if rows else slug),
                        "summary": summary,
                        "tags": list(dict.fromkeys(self.current["tags"])),
                        "source_url": self.current["url"],
                    }
                self.current = None


class DetailPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.article_depth = 0
        self.capture: str | None = None
        self.buffer: list[str] = []
        self.current_term = ""
        self.terms: dict[str, str] = {}
        self.in_table = False
        self.in_row = False
        self.row: list[str] = []
        self.tables: list[list[list[str]]] = []
        self.table_rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = values.get("name") or values.get("property") or ""
            content = values.get("content") or ""
            if key and content:
                self.meta[key] = clean_text(html.unescape(content))
        if tag == "article":
            self.article_depth += 1
            return
        if not self.article_depth:
            return
        if tag in {"dt", "dd"}:
            self.capture = tag
            self.buffer = []
        elif tag == "table":
            self.in_table = True
            self.table_rows = []
        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.row = []
        elif tag in {"th", "td"} and self.in_row:
            self.capture = "cell"
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.article_depth and tag != "article":
            return
        if tag == "dt" and self.capture == "dt":
            self.current_term = clean_text(" ".join(self.buffer))
            self.capture = None
        elif tag == "dd" and self.capture == "dd":
            if self.current_term:
                self.terms[self.current_term] = clean_text(" ".join(self.buffer))
            self.current_term = ""
            self.capture = None
        elif tag in {"th", "td"} and self.capture == "cell":
            self.row.append(clean_text(" ".join(self.buffer)))
            self.capture = None
        elif tag == "tr" and self.in_row:
            if any(self.row):
                self.table_rows.append(self.row)
            self.in_row = False
            self.row = []
        elif tag == "table" and self.in_table:
            if self.table_rows:
                self.tables.append(self.table_rows)
            self.in_table = False
            self.table_rows = []
        elif tag == "article":
            self.article_depth = max(0, self.article_depth - 1)


def parse_catalog_list(page: str) -> dict[str, dict[str, Any]]:
    parser = CatalogListParser()
    parser.feed(page)
    parser.close()
    return parser.records


def parse_detail_page(page: str) -> dict[str, Any]:
    parser = DetailPageParser()
    parser.feed(page)
    parser.close()
    return {
        "description": parser.meta.get("description", ""),
        "title": parser.meta.get("og:title", ""),
        "terms": parser.terms,
        "tables": parser.tables,
    }


def _flatten_numbers(value: Any) -> Iterable[float]:
    if isinstance(value, list):
        for item in value:
            yield from _flatten_numbers(item)
    else:
        parsed = number(value)
        if parsed is not None:
            yield parsed


def _normalize_bbox(value: Any) -> list[float]:
    if not isinstance(value, list) or not value or not isinstance(value[0], list):
        return []
    bbox = value[0]
    if len(bbox) == 4:
        return [float(item) for item in bbox]
    if len(bbox) >= 6:
        return [float(bbox[0]), float(bbox[1]), float(bbox[3]), float(bbox[4])]
    return []


def _self_url(item: dict[str, Any]) -> str:
    for link in item.get("links", []):
        if link.get("rel") == "self":
            return clean_text(link.get("href"))
    return ""


def _source_url(item: dict[str, Any], slug: str) -> str:
    for link in item.get("links", []):
        href = clean_text(link.get("href"))
        if "/earth-engine/datasets/catalog/" in href:
            return href.split("#", 1)[0]
    return f"{CATALOG_URL}/{slug}"


def _normalize_band(band: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "name",
        "description",
        "gsd",
        "gee:wavelength",
        "gee:scale",
        "gee:offset",
        "gee:units",
        "gee:data_type",
        "common_name",
        "center_wavelength",
        "full_width_half_max",
    ):
        if key in band:
            result[key] = band[key]
    if band.get("gee:classes"):
        result["classes"] = band["gee:classes"]
    return result


def normalize_stac(item: dict[str, Any]) -> dict[str, Any]:
    stac_url = _self_url(item)
    slug = Path(urllib.parse.urlparse(stac_url).path).stem if stac_url else clean_text(item.get("id")).replace("/", "_")
    summaries = item.get("summaries") if isinstance(item.get("summaries"), dict) else {}
    bands = [
        _normalize_band(band)
        for band in summaries.get("eo:bands", [])
        if isinstance(band, dict) and band.get("name")
    ]
    gsd_values = list(_flatten_numbers(summaries.get("gsd", [])))
    for band in bands:
        gsd_values.extend(_flatten_numbers(band.get("gsd")))
    gsd_values = [value for value in gsd_values if value >= 0]

    temporal = item.get("extent", {}).get("temporal", {}).get("interval", [])
    interval = temporal[0] if temporal and isinstance(temporal[0], list) else []
    start_date = parse_date(interval[0]) if interval else ""
    end_date = parse_date(interval[1]) if len(interval) > 1 else ""
    providers = item.get("providers") if isinstance(item.get("providers"), list) else []
    primary_provider = next(
        (clean_text(provider.get("name")) for provider in providers if "host" not in provider.get("roles", [])),
        clean_text(providers[0].get("name")) if providers else "",
    )
    license_value = clean_text(item.get("license"))
    status = clean_text(item.get("gee:status")) or ("deprecated" if item.get("deprecated") else "unknown")

    return {
        "id": clean_text(item.get("id")),
        "slug": slug,
        "title": clean_text(item.get("title")),
        "description": clean_text(item.get("description")),
        "gee_type": clean_text(item.get("gee:type")),
        "stac_type": clean_text(item.get("type")),
        "status": status,
        "deprecated": bool(item.get("deprecated") or status == "deprecated"),
        "version": clean_text(item.get("version")),
        "start_date": start_date,
        "end_date": end_date,
        "bbox": _normalize_bbox(item.get("extent", {}).get("spatial", {}).get("bbox", [])),
        "categories": list(dict.fromkeys(clean_text(value) for value in item.get("gee:categories", []) if clean_text(value))),
        "keywords": list(dict.fromkeys(clean_text(value) for value in item.get("keywords", []) if clean_text(value))),
        "providers": providers,
        "primary_provider": primary_provider,
        "license": license_value,
        "open_license": is_open_license(license_value),
        "terms_of_use": clean_text(item.get("gee:terms_of_use")),
        "citation": clean_text(item.get("sci:citation")),
        "doi": clean_text(item.get("sci:doi")),
        "cadence": item.get("gee:interval") or {},
        "bands": bands,
        "gsd_min": min(gsd_values) if gsd_values else None,
        "gsd_max": max(gsd_values) if gsd_values else None,
        "schema": summaries.get("gee:schema", []),
        "visualizations": summaries.get("gee:visualizations", []),
        "properties": {key: value for key, value in summaries.items() if key not in {"eo:bands", "gee:schema", "gee:visualizations", "gsd"}},
        "source_url": _source_url(item, slug),
        "stac_url": stac_url,
        "localizations": {},
    }


def discover_stac_items(client: HttpClient, workers: int) -> tuple[list[str], int]:
    pending = [STAC_ROOT]
    visited: set[str] = set()
    item_urls: set[str] = set()
    catalog_count = 0
    while pending:
        batch = [url for url in pending if url not in visited]
        pending = []
        if not batch:
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(client.fetch_json, url): url for url in batch}
            for future in concurrent.futures.as_completed(future_map):
                url = future_map[future]
                visited.add(url)
                data = future.result()
                catalog_count += 1
                for link in data.get("links", []):
                    if link.get("rel") != "child" or not link.get("href"):
                        continue
                    href = urllib.parse.urljoin(url, link["href"])
                    if href.rstrip("/").endswith("catalog.json"):
                        if href not in visited:
                            pending.append(href)
                    elif href.endswith(".json"):
                        item_urls.add(href)
    return sorted(item_urls), catalog_count


def fetch_stac_records(client: HttpClient, urls: list[str], workers: int) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(client.fetch_json, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_map):
            url = future_map[future]
            try:
                records.append(normalize_stac(future.result()))
            except Exception as exc:
                errors.append(f"{url}: {exc}")
    records.sort(key=lambda record: record.get("id", "").casefold())
    return records, errors


def fetch_localized_lists(client: HttpClient) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    errors: list[str] = []
    for language in ("en", "zh-cn"):
        url = f"{CATALOG_URL}?hl={urllib.parse.quote(language)}"
        try:
            result[language] = parse_catalog_list(client.fetch_text(url))
        except Exception as exc:
            result[language] = {}
            errors.append(f"{url}: {exc}")
    return result, errors


def fetch_localized_details(
    client: HttpClient,
    slugs: list[str],
    workers: int,
    language: str = "zh-cn",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    details: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def fetch_one(slug: str) -> tuple[str, dict[str, Any]]:
        url = f"{CATALOG_URL}/{urllib.parse.quote(slug, safe='_-.')}?hl={urllib.parse.quote(language)}"
        return slug, parse_detail_page(client.fetch_text(url))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fetch_one, slug): slug for slug in slugs}
        for future in concurrent.futures.as_completed(future_map):
            slug = future_map[future]
            try:
                key, detail = future.result()
                details[key] = detail
            except Exception as exc:
                errors.append(f"{slug}: {exc}")
    return details, errors


def _existing_records(assets_dir: Path) -> dict[str, dict[str, Any]]:
    path = assets_dir / CATALOG_FILENAME
    if not path.exists():
        return {}
    try:
        return {record.get("slug", ""): record for record in read_jsonl_gz(path) if record.get("slug")}
    except Exception:
        return {}


def merge_localizations(
    records: list[dict[str, Any]],
    localized_lists: dict[str, dict[str, dict[str, Any]]],
    localized_details: dict[str, dict[str, Any]],
    existing: dict[str, dict[str, Any]],
) -> None:
    for record in records:
        slug = record["slug"]
        old_localizations = existing.get(slug, {}).get("localizations", {})
        localizations: dict[str, Any] = {}
        for language in ("en", "zh-cn"):
            list_value = localized_lists.get(language, {}).get(slug, {})
            previous = old_localizations.get(language, {}) if isinstance(old_localizations, dict) else {}
            value = {
                "title": clean_text(list_value.get("title")) or clean_text(previous.get("title")) or record["title"],
                "summary": clean_text(list_value.get("summary")) or clean_text(previous.get("summary")) or record["description"],
                "tags": list_value.get("tags") or previous.get("tags") or record["keywords"],
                "source_url": list_value.get("source_url") or previous.get("source_url") or record["source_url"],
                "translation_source": "google-ai-translation" if language == "zh-cn" else "original",
            }
            if language == "zh-cn":
                detail = localized_details.get(slug) or previous.get("detail") or {}
                if detail:
                    value["detail"] = detail
                    if detail.get("description"):
                        value["summary"] = detail["description"]
            localizations[language] = value
        record["localizations"] = localizations


def _field_completeness(records: list[dict[str, Any]]) -> dict[str, float]:
    fields = [
        "title",
        "description",
        "gee_type",
        "status",
        "start_date",
        "bbox",
        "primary_provider",
        "license",
        "bands",
        "source_url",
        "stac_url",
    ]
    if not records:
        return {field: 0.0 for field in fields}
    return {
        field: round(sum(1 for record in records if record.get(field) not in (None, "", [], {})) / len(records), 4)
        for field in fields
    }


def update_catalog(
    output_dir: str | Path | None = None,
    *,
    proxy: str | None = None,
    workers: int = 8,
    timeout: float = 40.0,
    retries: int = 3,
    include_localized_details: bool = True,
    allow_partial: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch, normalize, validate, and atomically replace the bundled catalog."""
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    assets_dir = Path(output_dir) if output_dir else DEFAULT_ASSETS_DIR
    assets_dir.mkdir(parents=True, exist_ok=True)
    work_dir = assets_dir / ".update-work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    client = HttpClient(proxy=proxy, timeout=timeout, retries=retries)
    existing = _existing_records(assets_dir)
    started = time.perf_counter()

    def report(message: str) -> None:
        if progress:
            progress(message)

    report("Discovering STAC catalog items")
    item_urls, stac_catalog_count = discover_stac_items(client, workers)
    report(f"Fetching {len(item_urls)} STAC dataset records")
    records, stac_errors = fetch_stac_records(client, item_urls, workers)
    report("Fetching English and Simplified Chinese catalog lists")
    localized_lists, localized_list_errors = fetch_localized_lists(client)
    detail_errors: list[str] = []
    details: dict[str, dict[str, Any]] = {}
    zh_slugs = sorted(localized_lists.get("zh-cn", {}))
    if include_localized_details and zh_slugs:
        report(f"Fetching {len(zh_slugs)} Simplified Chinese detail pages")
        details, detail_errors = fetch_localized_details(client, zh_slugs, workers, "zh-cn")

    total_stac = len(item_urls)
    if stac_errors and not allow_partial and len(stac_errors) > max(5, int(total_stac * 0.02)):
        raise RuntimeError(f"STAC fetch had {len(stac_errors)} failures; refusing partial update")
    if include_localized_details and detail_errors and not allow_partial:
        if len(detail_errors) > max(10, int(max(1, len(zh_slugs)) * 0.05)):
            raise RuntimeError(f"Localized detail fetch had {len(detail_errors)} failures; refusing partial update")

    merge_localizations(records, localized_lists, details, existing)
    report("Validating and writing catalog atomically")
    validation = validate_records(records)
    if not validation["valid"]:
        raise RuntimeError("Catalog validation failed: " + "; ".join(validation["errors"][:10]))

    catalog_path = work_dir / CATALOG_FILENAME
    write_jsonl_gz(catalog_path, records)
    text_catalog_path = work_dir / CATALOG_TEXT_FILENAME
    catalog_content_sha256 = write_catalog_text_transport(catalog_path, text_catalog_path)
    summary_path = work_dir / CATALOG_SUMMARY_FILENAME
    write_catalog_summary(summary_path, records)
    stac_slugs = {record["slug"] for record in records}
    en_slugs = set(localized_lists.get("en", {}))
    zh_slugs_set = set(localized_lists.get("zh-cn", {}))
    manifest = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "sources": {
            "stac": STAC_ROOT,
            "catalog_en": f"{CATALOG_URL}?hl=en",
            "catalog_zh_cn": f"{CATALOG_URL}?hl=zh-cn",
        },
        "languages": ["en", "zh-cn"],
        "record_count": len(records),
        "stac_item_url_count": len(item_urls),
        "stac_catalog_count": stac_catalog_count,
        "localized_list_counts": {
            "en": len(en_slugs),
            "zh-cn": len(zh_slugs_set),
        },
        "localized_detail_count": len(details),
        "localized_details_refreshed": include_localized_details,
        "differences": {
            "stac_not_in_english_html": sorted(stac_slugs - en_slugs),
            "english_html_not_in_stac": sorted(en_slugs - stac_slugs),
            "stac_not_in_chinese_html": sorted(stac_slugs - zh_slugs_set),
            "chinese_html_not_in_stac": sorted(zh_slugs_set - stac_slugs),
            "english_not_in_chinese_html": sorted(en_slugs - zh_slugs_set),
            "chinese_not_in_english_html": sorted(zh_slugs_set - en_slugs),
        },
        "errors": {
            "stac": stac_errors,
            "localized_lists": localized_list_errors,
            "localized_details": detail_errors,
        },
        "field_completeness": _field_completeness(records),
        "validation": validation,
        "catalog_sha256": sha256_file(catalog_path),
        "catalog_content_sha256": catalog_content_sha256,
        "catalog_text_sha256": sha256_file(text_catalog_path),
        "catalog_summary_sha256": sha256_file(summary_path),
        "proxy_used": bool(proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
    }
    manifest_path = work_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    os.replace(catalog_path, assets_dir / CATALOG_FILENAME)
    os.replace(text_catalog_path, assets_dir / CATALOG_TEXT_FILENAME)
    os.replace(summary_path, assets_dir / CATALOG_SUMMARY_FILENAME)
    os.replace(manifest_path, assets_dir / MANIFEST_FILENAME)
    shutil.rmtree(work_dir, ignore_errors=True)
    report(f"Catalog update complete: {len(records)} records")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    parser.add_argument("--proxy", help="HTTP/HTTPS proxy, for example http://127.0.0.1:7897")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--skip-localized-details", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        manifest = update_catalog(
            output_dir=args.output_dir,
            proxy=args.proxy,
            workers=args.workers,
            timeout=args.timeout,
            retries=args.retries,
            include_localized_details=not args.skip_localized_details,
            allow_partial=args.allow_partial,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {
                "success": True,
                "generated_at": manifest["generated_at"],
                "record_count": manifest["record_count"],
                "ready_count": manifest["validation"]["status_counts"].get("ready", 0),
                "localized_list_counts": manifest["localized_list_counts"],
                "localized_detail_count": manifest["localized_detail_count"],
                "error_counts": {key: len(value) for key, value in manifest["errors"].items()},
                "duration_seconds": manifest["duration_seconds"],
                "catalog_sha256": manifest["catalog_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
