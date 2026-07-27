# Query Guide

Translate natural language into explicit constraints before using free-text
ranking.

| User intent | Query option |
|---|---|
| China / 中国 | `--region china` |
| A custom study area | `--bbox min_lon,min_lat,max_lon,max_lat` |
| Must cover the whole study area | add `--require-full-coverage` |
| 10 m or finer | `--max-gsd 10` |
| Available during a study period | `--temporal-start YYYY-MM-DD --temporal-end YYYY-MM-DD` |
| Must cover the whole study period | add `--require-full-temporal` |
| Sentinel-2 / Landsat | `--tag sentinel` or `--tag landsat` |
| Required bands | `--band B4,B8` |
| Band meaning/description | `--band-text "red edge"` |
| Matched band must be 20 m or finer | `--max-band-gsd 20` with `--band` or `--band-text` |
| Publicly licensed only | `--open-license` and inspect the original terms |
| Current production data | default `--status ready` |
| Include beta/deprecated | `--status all` |

Common query expansions:

- `surface reflectance` / `地表反射率` expands to SR, reflectance, optical,
  Sentinel, Landsat, and satellite imagery terms.
- `optical` / `光学` expands to multispectral, reflectance, Sentinel, and
  Landsat.
- `radar` / `雷达` expands to SAR, synthetic aperture, and Sentinel-1.
- `DEM` / `高程` expands to elevation, topography, DSM, and DTM.
- `NDVI` / `植被指数` expands to vegetation index, NDVI, and EVI.

The engine ranks by asset ID, title, tags, categories, bands, provider,
resolution, localized summary, and English description. Hard filters always run
before ranking, and every result includes a `why` list.

For `--require-full-coverage`, the engine excludes suspicious, invalid, and
uncertain audited bboxes. This avoids treating a coarse collection envelope as
continuous coverage. `--include-suspect-bbox` restores raw STAC behavior for
diagnostics only.

`--max-gsd` tests the finest native resolution anywhere in a dataset.
`--max-band-gsd` tests the bands selected by `--band` or `--band-text`; use the
latter for requests such as "red-edge bands at 20 m or finer". Do not treat a
10 m visible band as evidence that a 20 m red-edge band is also 10 m.
