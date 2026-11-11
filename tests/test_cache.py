from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from lead_radar.cache import ResponseCache, cache_key

PARAMS = {"engine": "google_maps", "type": "search", "q": "roofing contractor", "start": 20}


def test_key_ignores_api_key_and_param_order() -> None:
    reordered = {"start": 20, "q": "roofing contractor", "type": "search", "engine": "google_maps"}
    assert cache_key(PARAMS) == cache_key(reordered)
    assert cache_key(PARAMS) == cache_key({**PARAMS, "api_key": "secret", "no_cache": True})


def test_key_changes_with_any_search_param() -> None:
    assert cache_key(PARAMS) != cache_key({**PARAMS, "start": 40})
    assert cache_key(PARAMS) != cache_key({**PARAMS, "q": "plumber"})


def test_round_trip_and_layout(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.get(PARAMS) is None

    cache.put({**PARAMS, "api_key": "secret"}, {"local_results": [{"title": "Acme"}]})

    assert cache.get(PARAMS) == {"local_results": [{"title": "Acme"}]}
    stored = tmp_path / "google_maps" / f"{cache_key(PARAMS)}.json"
    assert stored.is_file()
    assert "secret" not in stored.read_text("utf-8"), "the API key must never be written to disk"
    assert not list(tmp_path.rglob("*.tmp"))


def test_expired_entries_are_misses(tmp_path: Path) -> None:
    ResponseCache(tmp_path).put(PARAMS, {"ok": True})
    assert ResponseCache(tmp_path, ttl=timedelta(hours=1)).get(PARAMS) == {"ok": True}
    assert ResponseCache(tmp_path, ttl=timedelta(seconds=-1)).get(PARAMS) is None


def test_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    cache.put(PARAMS, {"ok": True})
    (tmp_path / "google_maps" / f"{cache_key(PARAMS)}.json").write_text("{not json", "utf-8")
    assert cache.get(PARAMS) is None


def test_summary_and_clear(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.summary() == {}
    cache.put(PARAMS, {"ok": True})
    cache.put({**PARAMS, "start": 40}, {"ok": True})
    cache.put({"engine": "google_maps_reviews", "data_id": "0x1:0x2"}, {"reviews": []})

    summary = cache.summary()
    assert summary["google_maps"][0] == 2
    assert summary["google_maps_reviews"][0] == 1
    assert cache.clear() == 3
    assert cache.summary() == {}
