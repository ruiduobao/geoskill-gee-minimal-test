# Normalized Record Schema

Each line in `assets/catalog.jsonl.gz` is one normalized STAC item.

| Field | Meaning |
|---|---|
| `id` | Earth Engine asset ID, such as `COPERNICUS/S2_SR_HARMONIZED` |
| `slug` | Catalog URL slug, usually the asset ID with `/` replaced by `_` |
| `title`, `description` | English STAC metadata |
| `gee_type` | `image`, `image_collection`, `table`, `table_collection`, or `bigquery_table` |
| `status` | Source status such as `ready`, `beta`, or `deprecated` |
| `start_date`, `end_date` | ISO dates derived from STAC temporal extent |
| `bbox` | STAC outer envelope `[min_lon, min_lat, max_lon, max_lat]`; not proof of continuous coverage |
| `primary_provider`, `providers` | Provider names, roles, and URLs |
| `keywords`, `categories` | Source tags and Earth Engine categories |
| `license`, `open_license` | STAC license and conservative recognized-open marker |
| `terms_of_use`, `citation`, `doi` | Usage and scholarly provenance |
| `cadence` | Source `gee:interval` value when available |
| `bands` | Band objects with name, description, wavelength, GSD, scale, offset, units, and classes |
| `gsd_min`, `gsd_max` | Minimum and maximum native pixel size in meters |
| `schema`, `visualizations`, `properties` | Additional STAC summaries preserved for explanation |
| `source_url`, `stac_url` | Official HTML and STAC provenance |
| `localizations.en`, `localizations.zh-cn` | Localized title, summary, tags, detail tables, source URL, and translation provenance |
| `audit` | Applied spatial audit with quality, reason, evidence, source review, and optional manual curation |

`bbox_quality` is one of `likely_valid`, `confirmed_suspicious`, `uncertain`,
`invalid`, or `unreviewed`. Confirmed and uncertain envelopes are excluded from
strict full-coverage searches by default. `audit-curation.json` contains the
durable reviewed decisions; `audit-overrides.json` contains the generated
catalog-wide audit output.

`localizations.zh-cn.translation_source` is `google-ai-translation` because
Google marks the Chinese Developers pages as machine translated. English STAC
values remain authoritative for numeric and licensing facts.
