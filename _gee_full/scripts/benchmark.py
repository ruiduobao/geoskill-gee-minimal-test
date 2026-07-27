#!/usr/bin/env python3
"""Compare deterministic catalog retrieval with a documented direct-AI baseline."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from gee_catalog import Catalog, SearchOptions


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_benchmark(catalog: Catalog, cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        raw_options = dict(case["options"])
        for key in ("dataset_types", "tags", "categories", "bands"):
            if key in raw_options:
                raw_options[key] = set(raw_options[key])
        options = SearchOptions(**raw_options, query=case["query"])
        started = time.perf_counter()
        results = catalog.search(options)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        result_ids = [item["id"] for item in results]
        expected = set(case.get("expected", []))
        baseline = case.get("direct_ai_baseline", [])
        baseline_found = [asset_id for asset_id in baseline if catalog.get(asset_id)]
        baseline_ready = [
            asset_id for asset_id in baseline_found if catalog.get(asset_id).get("status") == "ready"
        ]
        rows.append(
            {
                "name": case["name"],
                "query": case["query"],
                "skill_top_ids": result_ids,
                "skill_hit_expected_top5": bool(expected.intersection(result_ids[:5])),
                "skill_query_ms": elapsed_ms,
                "baseline_ids": baseline,
                "baseline_found_in_current_catalog": baseline_found,
                "baseline_ready_count": len(baseline_ready),
                "baseline_has_expected": bool(expected.intersection(baseline)),
                "notes": "Direct-AI baseline is a fixed common-knowledge candidate list; it is not a second model call.",
            }
        )
    return {
        "method": "deterministic catalog query vs fixed direct-AI-style candidate lists",
        "catalog_generated_at": catalog.manifest.get("generated_at"),
        "case_count": len(rows),
        "skill_expected_hit_rate": round(sum(row["skill_hit_expected_top5"] for row in rows) / max(1, len(rows)), 3),
        "baseline_expected_hit_rate": round(sum(row["baseline_has_expected"] for row in rows) / max(1, len(rows)), 3),
        "mean_skill_query_ms": round(sum(row["skill_query_ms"] for row in rows) / max(1, len(rows)), 3),
        "rows": rows,
    }


def markdown(report: dict) -> str:
    lines = [
        "# GEE Skill vs Direct-AI Baseline",
        "",
        "The baseline is a fixed list of common answers a general AI often gives without a live catalog. It is intentionally not presented as an independent model evaluation.",
        "",
        f"- Catalog snapshot: `{report.get('catalog_generated_at')}`",
        f"- Skill expected hit@5: **{report.get('skill_expected_hit_rate', 0):.1%}**",
        f"- Baseline expected hit rate: **{report.get('baseline_expected_hit_rate', 0):.1%}**",
        f"- Mean skill query time: **{report.get('mean_skill_query_ms')} ms**",
        "",
        "| Case | Skill top result | Expected hit@5 | Baseline candidates found | Baseline ready |",
        "|---|---|---:|---:|---:|",
    ]
    for row in report["rows"]:
        top = row["skill_top_ids"][0] if row["skill_top_ids"] else "none"
        lines.append(
            f"| {row['name']} | `{top}` | {'yes' if row['skill_hit_expected_top5'] else 'no'} | {len(row['baseline_found_in_current_catalog'])} | {row['baseline_ready_count']} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "The skill is stronger when the request contains hard constraints: it returns current asset IDs, checks status and time/space overlap, exposes provenance, and can be rerun with identical inputs. A direct AI answer remains useful for brainstorming but can name deprecated IDs, omit spatial or temporal mismatches, and cannot reproduce the same ranked result without the index.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, default=Path(__file__).resolve().parent.parent / "assets")
    parser.add_argument("--cases", type=Path, default=Path(__file__).resolve().parents[3] / "benchmarks" / "cases.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[3] / "reports" / "benchmark.json")
    args = parser.parse_args()
    report = run_benchmark(Catalog.load(args.assets_dir), load_cases(args.cases))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
