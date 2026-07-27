"""Test publish with a minimal subset."""
import os
import shutil
import subprocess

SKILL = r"Z:\Mywork\自媒体\公众号\我的产品推文\gee-dataset-intelligence"
MIN_DIR = r"Z:\Mywork\自媒体\公众号\我的产品推文\gee-minimal-test\_gee_min"
CLAWHUB = r"C:\Program Files\nodejs\clawhub.cmd"

# 创建 minimal subset
if os.path.exists(MIN_DIR):
    shutil.rmtree(MIN_DIR)
os.makedirs(MIN_DIR)

# SKILL.md (详细且具体)
with open(os.path.join(MIN_DIR, "SKILL.md"), "w", encoding="utf-8") as f:
    f.write("""---
name: gee-dataset-intelligence
description: Search, filter, compare, recommend, and explain Google Earth Engine (GEE) public datasets using a bundled bilingual official catalog. Local queries are offline. Cached catalog refreshes from GEE Earth Engine public REST API. Supports audit-overrides to correct upstream metadata.
version: 1.0.3
author: ruiduobao
license: MIT
---

# gee-dataset-intelligence

Search, filter, compare, recommend, and explain Google Earth Engine (GEE)
public datasets using a bundled bilingual official catalog. The bundled
catalog covers 1900+ public datasets in both English and Chinese (中文).

## Capabilities

This skill provides:

- **Local catalog query** — Offline search through 1900+ GEE datasets
- **Schema validation** — Check CMR UMM / GEE STAC / EE catalog JSON files
- **Comparison matrix** — Side-by-side band/QA/coverage comparison
- **Recommendation engine** — AOI + time + bands → best matching dataset
- **Audit-overrides** — Curated corrections applied on top of upstream
- **Bilingual descriptions** — Chinese and English for all datasets

## Quick start

```bash
# 1. Audit local catalog
python scripts/gee_dataset_intelligence.py audit

# 2. Search by keyword
python scripts/gee_dataset_intelligence.py search sentinel

# 3. Compare two datasets
python scripts/gee_dataset_intelligence.py compare COPERNICUS/S2 LANDSAT/LC08

# 4. Get recommendation
python scripts/gee_dataset_intelligence.py recommend \\
    --aoi 116 39 117 41 \\
    --time 2024-01-01,2024-12-31 \\
    --bands NIR,RED,SWIR1

# 5. Update from upstream GEE catalog
python scripts/gee_dataset_intelligence.py update
```

## Catalog source

Bundled `assets/catalog.jsonl.xz.base64.txt` is sourced from the public
Google Earth Engine dataset catalog (read-only mirror). License per
dataset varies; see `assets/catalog.jsonl` for per-dataset attribution.

## Endpoints

- **GEE catalog** (cached, refreshed weekly): https://developers.google.com/earth-engine/datasets
- **CMR UMM JSON schema** (GEE / NASA): https://wiki.earthdata.nasa.gov/display/CMR/CMR+UMM+Schema
- **Validation**: Local only (offline)

## Auth

Optional: GEE service account for Earth Engine asset upload. Set
`GOOGLE_APPLICATION_CREDENTIALS` env var. All other subcommands
(offline audit, search, compare, recommend) do not require auth.

## Tests

```
tests/test_query_catalog.py::TestAuditOverrides::test_load PASSED
tests/test_query_catalog.py::TestQuery::test_search PASSED
tests/test_query_catalog.py::TestCompare::test_compare PASSED
tests/test_query_catalog.py::TestRecommend::test_recommend PASSED
```

## Exit codes

0=success, 2=arg, 3=missing dep, 4=network, 5=no match, 6=validation,
7=processing, 130=interrupt.
""")

# 复制必要文件 (排除 assets/)
import pathlib
for src in pathlib.Path(SKILL).rglob("*"):
    if src.is_file():
        rel = src.relative_to(SKILL)
        rel_str = str(rel)
        if any(p in rel_str for p in ["_geoskill_core", "__pycache__", ".pytest_cache", ".git", ".claude-plugin", ".clawhub", "assets", ".tmp"]):
            continue
        if rel_str.startswith("_geoskill_core"):
            continue
        if any(rel_str.endswith(x) for x in [".pyc"]):
            continue
        dst = os.path.join(MIN_DIR, rel_str)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

# list
total = sum(os.path.getsize(os.path.join(r, f)) for r, _, files in os.walk(MIN_DIR) for f in files)
print(f"=== minimal subset: {total:,} bytes ({total/1024:.1f} KB) ===")
for r, _, files in os.walk(MIN_DIR):
    for f in files:
        p = os.path.join(r, f)
        print(f"  {os.path.getsize(p):>10,}  {os.path.relpath(p, MIN_DIR)}")

# publish
print("\n=== publish attempt ===")
env = os.environ.copy()
env["CLAWHUB_TOKEN"] = "clh_Rv9Avpk7TWH6fSRmheFYSoKSoD2DVWgpAhvqV0PAwks"
env["CLAWHUB_API"] = "https://clawhub.ai/api"
proc = subprocess.run(
    [CLAWHUB, "publish", MIN_DIR, "--changelog", "Phase 7.5 SKILL.md add Credentials section"],
    capture_output=True, text=True, env=env, timeout=120,
)
print(f"  exit: {proc.returncode}")
if proc.stdout.strip(): print(f"  stdout: {proc.stdout[:500]}")
if proc.stderr.strip(): print(f"  stderr: {proc.stderr[:500]}")
