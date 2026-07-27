#!/usr/bin/env python3
"""Audit GEE spatial extents with heuristics, an LLM, and source evidence."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gee_catalog import Catalog, DEFAULT_ASSETS_DIR, clean_text, normalize_text


DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_API_STYLE = "openai"
USER_AGENT = "gee-dataset-intelligence-audit/1.0"
GLOBAL_BBOX = [-180.0, -90.0, 180.0, 90.0]
AI_CLASSIFICATIONS = {"confirmed_suspicious", "likely_valid", "uncertain", "not_bbox_issue"}
FINAL_QUALITIES = {"unreviewed", "likely_valid", "confirmed_suspicious", "uncertain", "invalid"}

REGION_TERMS = {
    "australia": "Australia",
    "canada": "Canada",
    "china": "China",
    "england": "England",
    "estonia": "Estonia",
    "finland": "Finland",
    "germany": "Germany",
    "greenland": "Greenland",
    "iran": "Iran",
    "japan": "Japan",
    "latvia": "Latvia",
    "mexico": "Mexico",
    "netherlands": "Netherlands",
    "new zealand": "New Zealand",
    "scotland": "Scotland",
    "slovakia": "Slovakia",
    "spain": "Spain",
    "united kingdom": "United Kingdom",
    "united states": "United States",
    "wales": "Wales",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_global_bbox(bbox: list[float]) -> bool:
    if len(bbox) != 4:
        return False
    return bbox[0] <= -179.9 and bbox[1] <= -89.9 and bbox[2] >= 179.9 and bbox[3] >= 89.9


def searchable_text(record: dict[str, Any]) -> str:
    localized = record.get("localizations", {}).get("en", {})
    return normalize_text(
        " ".join(
            [
                record.get("title", ""),
                record.get("description", ""),
                record.get("primary_provider", ""),
                " ".join(record.get("keywords", [])),
                localized.get("summary", ""),
            ]
        )
    )


def heuristic_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        bbox = record.get("bbox") or []
        text = searchable_text(record)
        reasons: list[str] = []
        severity = 0
        if len(bbox) != 4 or any(not isinstance(value, (int, float)) for value in bbox):
            reasons.append("bbox is missing or malformed")
            severity += 5
        elif bbox[0] >= bbox[2] or bbox[1] >= bbox[3] or bbox[0] < -180 or bbox[2] > 180 or bbox[1] < -90 or bbox[3] > 90:
            reasons.append("bbox coordinates are invalid")
            severity += 5

        mentioned = [label for term, label in REGION_TERMS.items() if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text)]
        if is_global_bbox(bbox) and mentioned:
            reasons.append("regional term appears with a global bbox: " + ", ".join(dict.fromkeys(mentioned)))
            severity += 3
        elif len(bbox) == 4 and mentioned and bbox[2] - bbox[0] > 180:
            reasons.append("regional term appears with a transcontinental bbox: " + ", ".join(dict.fromkeys(mentioned)))
            severity += 3
        if is_global_bbox(bbox) and re.search(r"\b(\d+(?:\.\d+)?\s*m|1m|5m|10m)\b", text) and re.search(r"\b(dem|dsm|dtm|terrain|lidar)\b", text):
            reasons.append("local-scale terrain product appears to have a global bbox")
            severity += 2
        if bbox and not is_global_bbox(bbox) and (bbox[2] - bbox[0] > 350 or bbox[3] - bbox[1] > 170):
            reasons.append("bbox is nearly global and should be checked against the source description")
            severity += 1

        if reasons:
            candidates.append(
                {
                    "id": record.get("id"),
                    "title": record.get("title"),
                    "description": clean_text(record.get("description"))[:1200],
                    "bbox": bbox,
                    "type": record.get("gee_type"),
                    "provider": record.get("primary_provider"),
                    "tags": record.get("keywords", [])[:30],
                    "source_url": record.get("source_url"),
                    "heuristic_reasons": reasons,
                    "severity": severity,
                }
            )
    return sorted(candidates, key=lambda item: (-item["severity"], item["id"] or ""))


def _json_from_text(text: str) -> Any:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    fallback: Any = None
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if fallback is None:
            fallback = value
        if isinstance(value, dict) and value.get("id") and value.get("classification"):
            return value
        if isinstance(value, list) and any(
            isinstance(item, dict) and item.get("id") for item in value
        ):
            return value
    if fallback is not None:
        return fallback
    raise json.JSONDecodeError("No JSON value found", text, 0)


def _api_endpoint(base_url: str, api_style: str) -> str:
    base = base_url.rstrip("/")
    if api_style == "openai":
        return base if base.endswith("/chat/completions") else base + "/chat/completions"
    if api_style == "anthropic":
        return base if base.endswith("/v1/messages") else base + "/v1/messages"
    raise ValueError(f"Unsupported API style: {api_style}")


def _response_text(response_data: dict[str, Any], api_style: str) -> str:
    if api_style == "openai":
        content = response_data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            return "".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        return str(content or "")
    content = response_data.get("content", [])
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )


def llm_batch(
    api_key: str,
    items: list[dict[str, Any]],
    *,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    api_style: str = DEFAULT_API_STYLE,
    timeout: float = 120.0,
) -> list[dict[str, Any]]:
    prompt = {
        "task": "Audit whether each Google Earth Engine STAC spatial bbox is credible.",
        "rules": [
            "Use only the supplied metadata; do not browse or invent facts.",
            "A global bbox is suspicious only when the title/description clearly says the product is regional or local.",
            "A global bbox can be valid for a truly global product.",
            "Return one JSON object per input item with exactly: id, classification, confidence, reason, review_priority.",
            "classification must be one of confirmed_suspicious, likely_valid, uncertain, not_bbox_issue.",
            "confidence must be a number from 0 to 1.",
        ],
        "items": items,
    }
    system = "You are a geospatial metadata quality auditor. Return strict JSON only."
    user = json.dumps(prompt, ensure_ascii=False)
    if api_style == "openai":
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max(1200, len(items) * 220),
        }
        headers = {"Authorization": f"Bearer {api_key}"}
    elif api_style == "anthropic":
        payload = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max(1200, len(items) * 220),
        }
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        raise ValueError(f"Unsupported API style: {api_style}")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _api_endpoint(base_url, api_style),
        data=body,
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_data = json.loads(response.read().decode("utf-8"))
    content = _response_text(response_data, api_style)
    parsed = _json_from_text(content)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("LLM response is not a JSON array")
    return [item for item in parsed if isinstance(item, dict)]


def validate_ai_response(
    requested: list[dict[str, Any]], returned: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    requested_ids = {str(item["id"]) for item in requested}
    accepted: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for index, result in enumerate(returned):
        asset_id = clean_text(result.get("id"))
        if not asset_id:
            errors.append(f"response item {index} has no id")
            continue
        if asset_id not in requested_ids:
            errors.append(f"response contains unknown id: {asset_id}")
            continue
        if asset_id in accepted:
            errors.append(f"response contains duplicate id: {asset_id}")
            continue
        classification = clean_text(result.get("classification"))
        if classification not in AI_CLASSIFICATIONS:
            errors.append(f"invalid classification for {asset_id}: {classification or '<missing>'}")
            continue
        try:
            confidence = float(result.get("confidence"))
        except (TypeError, ValueError):
            errors.append(f"invalid confidence for {asset_id}")
            continue
        if not 0 <= confidence <= 1:
            errors.append(f"confidence out of range for {asset_id}: {confidence}")
            continue
        accepted[asset_id] = {
            "id": asset_id,
            "classification": classification,
            "confidence": confidence,
            "reason": clean_text(result.get("reason")),
            "review_priority": clean_text(result.get("review_priority")),
        }
    return accepted, errors


class SourcePageParser:
    """Extract a bounded source-page excerpt without preserving raw HTML."""

    def __init__(self) -> None:
        self.title = ""
        self.description = ""
        self.text_parts: list[str] = []
        self._capture: str | None = None
        self._buffer: list[str] = []

    def feed(self, html_text: str) -> None:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
        if title_match:
            self.title = clean_text(html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))))
        description_match = re.search(
            r"<meta(?=[^>]+name=[\"']description[\"'])(?=[^>]+content=[\"'](.*?)[\"'])[^>]*>",
            html_text,
            re.I | re.S,
        )
        if description_match:
            self.description = clean_text(html.unescape(re.sub(r"<[^>]+>", " ", description_match.group(1))))
        body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>", " ", html_text, flags=re.I)
        body = re.sub(r"\s+", " ", html.unescape(body))
        self.text_parts = [body[:6000]]

    def result(self) -> dict[str, Any]:
        return {"page_title": self.title, "meta_description": self.description, "visible_excerpt": self.text_parts[0] if self.text_parts else ""}


def fetch_source_review(url: str, language: str = "en", timeout: float = 45.0) -> dict[str, Any]:
    target = url + ("&" if "?" in url else "?") + f"hl={language}"
    request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
        parser = SourcePageParser()
        parser.feed(page)
        return {"url": target, "success": True, "fetched_at": utc_now(), **parser.result()}
    except Exception as exc:
        return {"url": target, "success": False, "error": str(exc), "fetched_at": utc_now()}


def review_against_source(candidate: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    evidence = clean_text(" ".join([source.get("page_title", ""), source.get("meta_description", "")]))
    text = normalize_text(evidence)
    title = normalize_text(candidate.get("title", ""))
    region_terms = [term for term in REGION_TERMS if term in title or term in text]
    explicit_local_scope = bool(
        re.search(
            r"\b(covers?|covering|across|throughout|within|over|for|of)\b.{0,90}"
            r"\b(england|canada|australia|united states|u\.s\.|japan|netherlands|iran|"
            r"province|county|island|urban areas?|extraction areas?)\b",
            text,
        )
    ) or bool(re.search(r"\b(england|canada|united states|u\.s\.)['’]s\b", text))
    explicit_global_scope = bool(re.search(r"\b(global|worldwide|world[- ]wide|all land areas)\b", text))
    stac_global = is_global_bbox(candidate.get("bbox") or [])
    if stac_global and region_terms and explicit_local_scope and not explicit_global_scope:
        quality = "confirmed_suspicious"
        reason = "Official page explicitly limits the product to a named region while the STAC bbox is global."
    elif stac_global and explicit_global_scope and not explicit_local_scope:
        quality = "likely_valid"
        reason = "Official page text explicitly describes a global/worldwide product."
    else:
        quality = "uncertain"
        reason = "Source page does not provide enough unambiguous geographic wording for an automatic correction."
    return {
        "bbox_quality": quality,
        "source_review_reason": reason,
        "source_region_terms": region_terms,
        "source_evidence": evidence[:1200],
    }


def candidate_fingerprint(candidates: list[dict[str, Any]]) -> str:
    value = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_curation(assets_dir: Path) -> dict[str, dict[str, Any]]:
    path = assets_dir / "audit-curation.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", {})
    if not isinstance(records, dict):
        raise ValueError("audit-curation.json records must be an object")
    for asset_id, decision in records.items():
        if decision.get("bbox_quality") not in FINAL_QUALITIES:
            raise ValueError(f"Invalid curated bbox_quality for {asset_id}")
        if not clean_text(decision.get("reason")):
            raise ValueError(f"Curated decision has no reason: {asset_id}")
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def audit_catalog(
    assets_dir: Path = DEFAULT_ASSETS_DIR,
    report_dir: Path | None = None,
    *,
    api_key: str | None = None,
    api_base_url: str = DEFAULT_BASE_URL,
    api_model: str = DEFAULT_MODEL,
    api_style: str = DEFAULT_API_STYLE,
    batch_size: int = 12,
    max_candidates: int = 300,
    workers: int = 6,
    source_review: bool = False,
    resume: bool = True,
    write_overrides: bool = False,
) -> dict[str, Any]:
    if write_overrides and (not api_key or not source_review):
        raise ValueError(
            "Replacing audit overrides requires both LLM classification and source-page review"
        )
    catalog = Catalog.load(assets_dir)
    candidates = heuristic_candidates(catalog.records)[:max_candidates]
    fingerprint = candidate_fingerprint(candidates)
    provider = {"base_url": api_base_url, "model": api_model, "api_style": api_style}
    target_report = report_dir or assets_dir.parent.parent.parent / "reports"
    target_report.mkdir(parents=True, exist_ok=True)
    ai_results: dict[str, dict[str, Any]] = {}
    ai_errors: list[str] = []
    progress_path = target_report / "catalog-audit-progress.json"
    previous_path = target_report / "catalog-audit.json"
    resume_paths = [progress_path, previous_path]
    if resume:
        for resume_path in resume_paths:
            if not resume_path.exists():
                continue
            try:
                previous = json.loads(resume_path.read_text(encoding="utf-8"))
                same_catalog = previous.get("catalog_generated_at") == catalog.manifest.get("generated_at")
                same_provider = previous.get("ai_provider") == provider
                if not same_provider or not same_catalog:
                    continue
                source_records = previous.get("ai_results") or previous.get("records", {})
                for asset_id, value in source_records.items():
                    ai = value if value.get("classification") else value.get("ai") or {}
                    if ai.get("classification") in AI_CLASSIFICATIONS:
                        ai_results[asset_id] = ai
                break
            except Exception as exc:
                ai_errors.append(f"resume data ignored from {resume_path.name}: {exc}")
    if api_key and candidates:
        pending = [candidate for candidate in candidates if candidate["id"] not in ai_results]
        for offset in range(0, len(pending), batch_size):
            remaining = pending[offset : offset + batch_size]
            for attempt in range(3):
                if not remaining:
                    break
                try:
                    returned = llm_batch(
                        api_key,
                        remaining,
                        base_url=api_base_url,
                        model=api_model,
                        api_style=api_style,
                    )
                    accepted, validation_errors = validate_ai_response(remaining, returned)
                    ai_results.update(accepted)
                    ai_errors.extend(
                        f"batch {offset} attempt {attempt + 1}: {error}"
                        for error in validation_errors
                    )
                    next_remaining = [item for item in remaining if item["id"] not in accepted]
                    if len(next_remaining) == len(remaining):
                        ai_errors.append(f"batch {offset} attempt {attempt + 1}: no requested IDs returned")
                    remaining = next_remaining
                except Exception as exc:
                    ai_errors.append(f"batch {offset} attempt {attempt + 1}: {exc}")
                if remaining:
                    time.sleep(min(8, 2**attempt))
            if remaining:
                ai_errors.append("missing AI classifications: " + ", ".join(item["id"] for item in remaining))
            write_json(
                progress_path,
                {
                    "schema_version": 1,
                    "updated_at": utc_now(),
                    "candidate_fingerprint": fingerprint,
                    "catalog_generated_at": catalog.manifest.get("generated_at"),
                    "ai_provider": provider,
                    "ai_results": ai_results,
                },
            )
    else:
        ai_errors.append("LLM API key not supplied; AI classification skipped")

    missing_ids = [candidate["id"] for candidate in candidates if candidate["id"] not in ai_results]
    if api_key and missing_ids:
        raise RuntimeError(
            f"AI audit is incomplete ({len(ai_results)}/{len(candidates)}); "
            "final reports and overrides were not replaced. Re-run with resume enabled."
        )

    suspicious_ids = [
        candidate["id"]
        for candidate in candidates
        if ai_results.get(candidate["id"], {}).get("classification") in {"confirmed_suspicious", "uncertain"}
        or (not api_key and candidate["severity"] >= 5)
    ]
    source_reviews: dict[str, dict[str, Any]] = {}
    if source_review and suspicious_ids:
        by_id = {record.get("id"): record for record in catalog.records}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(fetch_source_review, by_id[asset_id]["source_url"]): asset_id for asset_id in suspicious_ids if by_id.get(asset_id)}
            for future in concurrent.futures.as_completed(future_map):
                asset_id = future_map[future]
                try:
                    source_reviews[asset_id] = future.result()
                except Exception as exc:
                    source_reviews[asset_id] = {"success": False, "error": str(exc)}

    curated = load_curation(assets_dir)
    overrides: dict[str, Any] = {}
    by_id = {record.get("id"): record for record in catalog.records}
    for candidate in candidates:
        asset_id = candidate["id"]
        ai = ai_results.get(asset_id, {})
        review = review_against_source(candidate, source_reviews[asset_id]) if asset_id in source_reviews and source_reviews[asset_id].get("success") else {}
        malformed = any("missing or malformed" in reason or "coordinates are invalid" in reason for reason in candidate["heuristic_reasons"])
        ai_quality = ai.get("classification") or "unreviewed"
        if ai_quality == "not_bbox_issue":
            ai_quality = "likely_valid"
        if ai_quality == "confirmed_suspicious":
            ai_quality = "uncertain"
        quality = "invalid" if malformed else review.get("bbox_quality") or ai_quality
        reason = review.get("source_review_reason") or ai.get("reason") or "; ".join(candidate["heuristic_reasons"])
        curated_decision = curated.get(asset_id)
        if curated_decision:
            quality = curated_decision["bbox_quality"]
            reason = curated_decision["reason"]
        methods = ["heuristic"]
        if api_key:
            methods.append("llm")
        if review:
            methods.append("source-page")
        override = {
            "bbox_quality": quality,
            "reason": reason,
            "audit_method": "+".join(methods),
            "audited_at": utc_now(),
            "heuristic_reasons": candidate["heuristic_reasons"],
            "ai": ai,
        }
        if review:
            override["source_review"] = {**source_reviews[asset_id], **review}
        if curated_decision:
            override["curation"] = curated_decision
            override["audit_method"] += "+manual-curation"
        overrides[asset_id] = override

    audit_payload = {
        "generated_at": utc_now(),
        "catalog_generated_at": catalog.manifest.get("generated_at"),
        "candidate_fingerprint": fingerprint,
        "ai_provider": provider,
        "record_count": len(catalog.records),
        "heuristic_candidate_count": len(candidates),
        "ai_result_count": len(ai_results),
        "ai_missing_count": len(missing_ids),
        "ai_errors": ai_errors,
        "source_review_count": len(source_reviews),
        "confirmed_suspicious_count": sum(1 for value in overrides.values() if value.get("bbox_quality") == "confirmed_suspicious"),
        "uncertain_count": sum(1 for value in overrides.values() if value.get("bbox_quality") == "uncertain"),
        "overrides_written": write_overrides,
        "records": overrides,
    }
    write_json(target_report / "catalog-audit.json", audit_payload)
    markdown_lines = [
        "# GEE Catalog Spatial Audit",
        "",
        f"- Catalog records: {len(catalog.records)}",
        f"- Heuristic candidates: {len(candidates)}",
        f"- LLM classifications: {len(ai_results)} ({api_model}, {api_style})",
        f"- Official source pages reviewed: {len(source_reviews)}",
        f"- Confirmed suspicious: {audit_payload['confirmed_suspicious_count']}",
        f"- Uncertain: {audit_payload['uncertain_count']}",
        "",
        "| Asset ID | Quality | Reason | Source |",
        "|---|---|---|---|",
    ]
    for asset_id, override in sorted(overrides.items()):
        if override.get("bbox_quality") in {"confirmed_suspicious", "uncertain", "invalid"}:
            source = override.get("source_review", {}).get("url", "")
            reason = override.get("reason") or "; ".join(override.get("heuristic_reasons", []))
            markdown_lines.append(f"| `{asset_id}` | `{override.get('bbox_quality')}` | {clean_text(reason)} | {source} |")
    (target_report / "catalog-audit.md").write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    if write_overrides:
        write_json(assets_dir / "audit-overrides.json", {"schema_version": 1, "generated_at": utc_now(), "records": overrides})
    if write_overrides and progress_path.exists():
        progress_path.unlink()
    return audit_payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--api-key-env", default="GEE_AUDIT_API_KEY")
    parser.add_argument("--base-url", default=os.environ.get("GEE_AUDIT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("GEE_AUDIT_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--api-style",
        choices=("openai", "anthropic"),
        default=os.environ.get("GEE_AUDIT_API_STYLE", DEFAULT_API_STYLE),
    )
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--max-candidates", type=int, default=600)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Send public candidate metadata to the configured LLM endpoint",
    )
    parser.add_argument(
        "--enable-source-review",
        action="store_true",
        help="Fetch official Google catalog pages for suspicious candidates",
    )
    parser.add_argument(
        "--write-overrides",
        action="store_true",
        help="Replace audit-overrides.json after complete LLM and source review",
    )
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env) if args.enable_llm else None
    if args.enable_llm and not api_key:
        parser.error(f"--enable-llm requires the {args.api_key_env} environment variable")
    if args.enable_llm:
        print(
            f"Network audit enabled: public catalog candidate metadata will be sent to "
            f"{args.base_url} using model {args.model}.",
            file=sys.stderr,
        )
    if args.enable_source_review:
        print(
            "Source review enabled: official developers.google.com catalog pages will be fetched.",
            file=sys.stderr,
        )
    if args.write_overrides and (not args.enable_llm or not args.enable_source_review):
        parser.error("--write-overrides requires --enable-llm and --enable-source-review")
    result = audit_catalog(
        args.assets_dir,
        args.report_dir,
        api_key=api_key,
        api_base_url=args.base_url,
        api_model=args.model,
        api_style=args.api_style,
        batch_size=args.batch_size,
        max_candidates=args.max_candidates,
        workers=args.workers,
        source_review=args.enable_source_review,
        resume=not args.no_resume,
        write_overrides=args.write_overrides,
    )
    print(json.dumps({key: result[key] for key in ("generated_at", "record_count", "heuristic_candidate_count", "ai_result_count", "ai_missing_count", "source_review_count", "confirmed_suspicious_count", "uncertain_count", "overrides_written", "ai_errors")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
