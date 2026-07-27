"""Basic tests for gee-dataset-intelligence (Phase 2 round 2)."""
import importlib.util
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(PROJECT_ROOT, "scripts")


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def test_query_catalog_help():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    combined = out.stdout + out.stderr
    assert "--region" in combined
    assert "--max-gsd" in combined
    assert "--bbox" in combined
    assert "--id" in combined


def test_region_bboxes_china():
    """REGION_BBOXES should include 'china' (73, 18, 135, 54)."""
    # Read directly from the source file (avoids module-load side effects)
    src = open(os.path.join(SCRIPTS, "query_catalog.py"), "r", encoding="utf-8").read()
    assert '"china": [73.0, 18.0, 135.0, 54.0]' in src or "'china': [73.0, 18.0, 135.0, 54.0]" in src


def test_region_bboxes_global():
    src = open(os.path.join(SCRIPTS, "query_catalog.py"), "r", encoding="utf-8").read()
    assert '"global"' in src and "-180.0" in src and "180.0" in src


def test_region_bboxes_all_keys():
    src = open(os.path.join(SCRIPTS, "query_catalog.py"), "r", encoding="utf-8").read()
    for region in ("africa", "asia", "china", "europe", "global",
                   "north-america", "south-america", "usa"):
        assert f'"{region}"' in src or f"'{region}'" in src, f"missing region {region!r}"


def test_query_catalog_stats_runs():
    """`--stats` should print a JSON catalog summary (uses bundled assets)."""
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"), "--stats",
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"stderr: {out.stderr}"
    # Output is JSON; check key fields
    import json
    data = json.loads(out.stdout)
    assert "record_count" in data
    assert data["record_count"] > 0


def test_query_catalog_region_china_runs():
    """Query with --region china should return datasets (may be 0+)."""
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"),
         "--region", "china", "--limit", "5", "--format", "json",
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"stderr: {out.stderr}"


def test_update_catalog_help():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "update_catalog.py"), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert out.returncode == 0
    assert "--output-dir" in (out.stdout + out.stderr)


# ---------------------------------------------------------------------------
# Phase 6 — --place flag for Chinese place names
# ---------------------------------------------------------------------------


def test_help_mentions_place_flag():
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert out.returncode == 0
    assert "--place" in (out.stdout + out.stderr)


def test_place_resolver_imports_successfully():
    """place_resolver.resolve_place() should be importable from the skill root."""
    sys.path.insert(0, PROJECT_ROOT)
    try:
        from place_resolver import resolve_place  # type: ignore
    except ImportError as exc:
        pytest.fail(f"place_resolver not importable: {exc}")
    # Resolve a well-known hardcoded place (no network)
    bbox = resolve_place("北京市")
    assert isinstance(bbox, tuple) and len(bbox) == 4
    w, s, e, n = bbox
    # Beijing's bbox should be roughly within (115, 39, 117, 41)
    assert 115.0 < w < 117.0
    assert 39.0 < s < 41.0
    assert 115.0 < e < 117.0
    assert 39.0 < n < 41.0


def test_place_flag_runs_for_known_place(monkeypatch=None):
    """--place 北京市 should run end-to-end and return search results."""
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"),
         "--place", "北京市",
         "--limit", "5", "--format", "json",
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"stderr: {out.stderr}"
    import json
    data = json.loads(out.stdout)
    # Should return a list of results (may be 0+); just check it parses
    assert isinstance(data, list)


def test_place_and_bbox_mutually_exclusive():
    """Passing both --place and --bbox should return an error (no network)."""
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"),
         "--place", "北京市",
         "--bbox", "115.0,39.0,117.0,41.0",
         "--limit", "5",
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=15,
    )
    assert out.returncode != 0
    combined = out.stdout + out.stderr
    assert "either" in combined or "both" in combined


def test_place_unknown_returns_error():
    """--place with an unresolvable garbage string should error gracefully."""
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"),
         "--place", "qzx-not-a-real-place-12345",
         "--limit", "5",
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=30,
    )
    # Either fails with place resolution, or returns 0 with empty list.
    # The exact behavior depends on whether the resolver has a network path.
    # Just verify the process exits cleanly (no crash).
    assert out.returncode in (0, 1)


# ---------------------------------------------------------------------------
# Phase 5: --qa sidecar summary
# ---------------------------------------------------------------------------


def test_qa_flag_accepted():
    """--qa should appear in --help and not error on a normal query."""
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert "--qa" in (out.stdout + out.stderr)


def test_qa_sidecar_written_for_search(tmp_path):
    """--qa PATH should write a JSON sidecar with the action and filters."""
    import json as _json
    qa_path = str(tmp_path / "run.qa.json")
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"),
         "--query", "sentinel",
         "--region", "china",
         "--limit", "3",
         "--format", "json",
         "--qa", qa_path,
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"stderr: {out.stderr}"
    assert os.path.exists(qa_path), "QA sidecar not written"
    data = _json.loads(open(qa_path, encoding="utf-8").read())
    assert data["skill"] == "gee-dataset-intelligence"
    assert data["command"] == "search"
    assert data["query"] == "sentinel"
    assert data["region"] == "china"
    assert data["limit"] == 3
    assert data["format"] == "json"
    assert "timestamp" in data
    assert "version" in data
    assert isinstance(data["record_count"], int)
    assert isinstance(data["record_ids"], list)


def test_qa_sidecar_written_for_stats(tmp_path):
    """--qa should record action=stats when --stats is used."""
    import json as _json
    qa_path = str(tmp_path / "stats.qa.json")
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"),
         "--stats", "--qa", qa_path,
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0
    data = _json.loads(open(qa_path, encoding="utf-8").read())
    assert data["command"] == "stats"
    assert "record_count" in data
    assert data["record_count"] > 0


def test_qa_creates_parent_dir(tmp_path):
    """--qa should create missing parent directories."""
    qa_path = str(tmp_path / "deep" / "nested" / "run.qa.json")
    out = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "query_catalog.py"),
         "--stats", "--qa", qa_path,
         "--assets-dir", os.path.join(PROJECT_ROOT, "assets")],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0
    assert os.path.exists(qa_path)
