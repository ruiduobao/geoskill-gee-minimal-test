# gee-dataset-intelligence

Search, filter, compare, recommend, and explain Google Earth Engine (GEE)
public datasets using a bundled bilingual official catalog.

## Capabilities

This skill provides:

- **Local dataset catalog query** (offline, 1900+ GEE datasets in bilingual)
- **Schema validation** (CMR UMM JSON, GEE STAC, EE catalog snapshot)
- **Comparison matrix** (between GEE datasets, band specs, providers)
- **Recommendation engine** (AOI + time + bands -> best dataset)
- **Audit-overrides** (curated corrections on top of upstream catalog)

## Usage

```bash
# 1. audit / refresh (offline)
python gee_dataset_intelligence.py audit

# 2. search by keyword
python gee_dataset_intelligence.py search "sentinel"

# 3. compare two datasets
python gee_dataset_intelligence.py compare COPERNICUS/S2 LANDSAT/LC08

# 4. get recommendation
python gee_dataset_intelligence.py recommend --aoi 116 39 117 41 --time 2024-01-01,2024-12-31 --bands nir,red
```

## Catalog sources

Bundled `assets/catalog.jsonl.xz.base64.txt` is sourced from GEE public
catalog snapshot (read-only mirror). License: GEE Terms of Service.

## Auth

Optional: GEE service account (Earth Engine asset upload). Set
`GOOGLE_APPLICATION_CREDENTIALS` env var. Local catalog queries do not
require auth.
