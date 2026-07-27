---
description: Search, filter, compare, recommend, and explain Google Earth Engine public
  datasets using a bundled bilingual official catalog. Local queries are offline.
  Catalog refresh explicitly fetches Google sources and writes catalog assets; optional
  spatial audit sends public catalog metadata to a configured third-party LLM and
  fetches Google pages only with explicit flags. Use for dataset selection, IDs, bands,
  resolution, coverage, licensing, citations, status, or refresh. 使用本地中英文官方目录检索、比较和解释
  GEE 数据集；联网更新和 AI 审计仅在明确调用时运行。
name: gee-dataset-intelligence
---

# GEE Dataset Intelligence

Use the bundled catalog index as the factual source for Earth Engine dataset
metadata. Use `scripts/query_catalog.py` for deterministic retrieval, then
explain the result in the user's language with official source URLs and the
Earth Engine constructor snippet. Do not invent bands, dates, licenses, or
coverage that are absent from the record.

## Workflow

1. Load the local index. If it is missing, stale, or the user explicitly asks
   for current data, run `scripts/update_catalog.py` or import
   `update_catalog.update_catalog()`.
2. Translate the user's intent into hard filters before ranking. Use
   `--region china` or `--bbox` for named areas, `--max-gsd` for the finest
   acceptable native resolution, `--temporal-start/--temporal-end` for overlap,
   and `--type`, `--tag`, `--band`, `--provider`, or `--open-license` when
   explicitly requested.
   Add `--require-full-coverage` for wording such as "covers China" or
   "global", and `--require-full-temporal` for wording such as "since 2000" or
   "throughout 2018-2024".
   Treat `bbox` as an outer envelope, not proof that every point inside has
   data. Strict full-coverage searches exclude records whose bbox audit is
   suspicious, uncertain, or invalid. Use `--include-suspect-bbox` only when
   the user explicitly wants raw STAC behavior and disclose the risk.
3. Use `--query` for remaining subject terms. The query engine expands common
   remote-sensing terms such as surface reflectance, optical, radar, DEM,
   precipitation, land cover, and vegetation index.
4. Return a short ranked list. Include ID, type, status, provider, time range,
   spatial extent, GSD, useful bands, license, code snippet, and source URL.
5. For comparisons, use `--compare` and present the differences rather than
   repeating full descriptions. For one asset, use `--id`.
6. State uncertainty explicitly. Chinese text is marked as Google AI
   translation; prefer the English STAC description when the translations
   conflict.

## English usage

Convert factual requirements into hard filters first. Use `--band` for exact
band names, and combine `--band-text` with `--max-band-gsd` when a requested
spectral property must meet a resolution limit. If the exact query returns no
results, state that clearly, then run a second relaxed query and label it as an
alternative rather than silently weakening the requirement.

```powershell
python scripts\query_catalog.py --query "surface reflectance" `
  --region china --type image_collection --max-gsd 10 `
  --temporal-start 2017-01-01 --require-full-coverage `
  --language zh-cn --limit 8

python scripts\query_catalog.py --id COPERNICUS/S2_SR_HARMONIZED --language zh-cn

python scripts\query_catalog.py --compare `
  COPERNICUS/S2_SR_HARMONIZED LANDSAT/LC08/C02/T1_L2 --format markdown

python scripts\query_catalog.py --query optical --region china `
  --type image_collection --band-text "red edge" --max-band-gsd 20 `
  --require-full-coverage --language en
```

The default JSON output is the stable machine-readable interface. Use
`--format markdown` for a user-facing list. A no-result response means the
hard filters were too restrictive; relax one filter at a time and report that
change rather than silently dropping constraints.

## 中文用法

先把用户要求转换为硬过滤条件，再进行自由文本排序。精确波段名使用
`--band`；需要按波段含义筛选时使用 `--band-text`；若该波段还必须满足
指定分辨率，同时使用 `--max-band-gsd`。严格查询无结果时，应明确说明
“没有完全符合项”，然后单独执行放宽条件的备选查询，不能静默删除约束。

```powershell
# 查询完整覆盖中国、10 米以内的地表反射率影像集合
python scripts\query_catalog.py --query "地表反射率" `
  --region china --type image_collection --max-gsd 10 `
  --require-full-coverage --language zh-cn --limit 8

# 查询红边波段本身不粗于 20 米的数据集
python scripts\query_catalog.py --query optical --region china `
  --type image_collection --band-text "red edge" --max-band-gsd 20 `
  --require-full-coverage --language zh-cn

# 比较两个土地覆盖产品的时间、分辨率、波段和更新频率
python scripts\query_catalog.py --compare `
  ESA/WorldCover/v200 MODIS/061/MCD12Q1 --language zh-cn
```

输出结果中的 `cadence` 表示更新或重访周期，`band_gsd_m` 表示各波段的
原生分辨率。`bbox` 只是外包矩形；要求“覆盖中国”时必须使用
`--require-full-coverage`，并保留默认的可疑范围排除规则。

## Update

The updater is an explicit network operation. It traverses the official Earth
Engine STAC tree, fetches every item,
parses the English and Chinese catalog lists, and optionally fetches all
Chinese detail pages. It writes `assets/catalog.jsonl.gz` and
the ClawHub-compatible text transport
`assets/catalog.jsonl.xz.base64.txt`, plus the human-readable bilingual table
`assets/catalog-summary.tsv`, then writes `assets/manifest.json`.
It refuses unexpectedly partial updates and records source-set differences
and field completeness.

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
python scripts\update_catalog.py --workers 8
```

For a quick daily refresh, skip translated detail pages:

```powershell
python scripts\update_catalog.py --skip-localized-details
```

The importable API is:

```python
from update_catalog import update_catalog

manifest = update_catalog(
    proxy="http://127.0.0.1:7897",
    include_localized_details=True,
)
```

PowerShell users can call `Update-GEECatalog` from the repository root
`Update-GEECatalog.ps1` wrapper. Set `GEE_AUDIT_API_KEY` and use
`Update-GEECatalog -Audit` to refresh the catalog and spatial audit in one
function call.

## Spatial audit

Run the audit after a catalog refresh or when a regional product appears in an
unrelated regional search. The default command is offline and applies only
heuristics plus existing manual curation. `--enable-llm` explicitly sends
public candidate metadata (ID, title, description, bbox, provider, and source
URL) to the configured LLM endpoint. `--enable-source-review` explicitly
fetches official `developers.google.com` pages. Never promote an LLM-only
verdict to `confirmed_suspicious`.

```powershell
# Offline audit: no web requests and no API key use
python scripts\audit_catalog.py

# Optional network audit: set GEE_AUDIT_API_KEY in this shell first
$env:GEE_AUDIT_BASE_URL = 'https://api.minimaxi.com/v1'
$env:GEE_AUDIT_MODEL = 'MiniMax-M3'
$env:GEE_AUDIT_API_STYLE = 'openai'
python scripts\audit_catalog.py `
  --enable-llm --enable-source-review --write-overrides
```

Without `--write-overrides`, audit commands write reports but preserve the
bundled corrections. Replacing `assets/audit-overrides.json` requires both
network review flags and one valid classification for every candidate.
Interrupted runs resume from
`reports/catalog-audit-progress.json`. Review `reports/catalog-audit.md`, then
record durable human decisions in `assets/audit-curation.json` and rerun.

Local query commands read catalog assets only. Refresh writes the catalog and
manifest under `assets/`; audit writes reports and changes bundled overrides
only with `--write-overrides` after both network reviews. Do not put API keys
in skill files, command examples, reports, or version control.

本地查询只读取目录文件，不联网。目录更新会访问 Google 官方数据源并写入
`assets/`；只有显式添加 `--enable-llm` 或 `--enable-source-review` 时，审计
才会调用第三方模型或抓取 Google 页面；只有再显式添加
`--write-overrides` 才会替换纠错文件。不要把 API Key 写入 Skill、报告或版本
控制。

## Data and provenance

The normalized records preserve asset ID, catalog slug, title, description,
GEE type, status, dates, bounding box, provider, keywords, categories, license,
terms, citation, DOI, cadence, bands, GSD, schema, visualizations, STAC URL,
HTML source URL, and bilingual localizations. The repository stores extracted
metadata and provenance URLs, not mirrored Google HTML pages.

Open `assets/catalog-summary.tsv` in a text editor or spreadsheet application
when a person needs to browse the catalog without decoding JSONL.

Read [schema.md](references/schema.md) when interpreting fields and
[query-guide.md](references/query-guide.md) when converting a natural
language request into filters.

## Failure handling

- If catalog files are absent, tell the user to run the updater.
- If a source request fails, retry with backoff and preserve the previous
  localized record when available.
- Refuse to replace a complete catalog when STAC failures exceed the safety
  threshold unless `--allow-partial` is explicit.
- Run `scripts/validate_catalog.py` after updates. Treat duplicate IDs, hash
  mismatches, low record counts, and missing required fields as errors.
- Treat `proprietary`, custom, or missing licenses as not verified open access;
  do not promise commercial usability.
- Do not infer continuous coverage from an image-collection bbox. For a
  high-stakes area, inspect image footprints or test the collection in Earth
  Engine after catalog filtering.


## Credentials

The **offline audit / query** workflow does **not** require any credentials.
Only the optional **LLM translation** of dataset descriptions (Chinese ↔
English) needs an OpenAI-compatible API key.

Resolution order for LLM key:

1. OPENAI_API_KEY env var
2. **Default** (geoskill-core credentials.py): empty — set the env var to enable

LLM translation is opt-in; the skill works fine without it (translations just
fall back to the local bilingual catalog). The key is never written to skill
source, sidecars, or logs.
