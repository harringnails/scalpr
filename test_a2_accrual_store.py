import json

import pytest

import a2_accrual_store as store


def test_dense_summary_is_authoritative_and_provenance_checked(tmp_path):
    path = tmp_path / "a2_summary_dense_v0.json"
    path.write_text(json.dumps({
        "endpoint_source": store.DENSE_ENDPOINT_SOURCE,
        "clean_a2_labelable_episode_count": 7,
    }))

    summary = store.load_dense_summary(path)

    assert summary["clean_a2_labelable_episode_count"] == 7
    assert summary["accrual_store"] == "dense_a2_v0"
    assert summary["accrual_summary_path"] == str(path)


def test_legacy_or_missing_summary_fails_closed(tmp_path):
    legacy = tmp_path / "a2_summary_v2.json"
    legacy.write_text(json.dumps({
        "endpoint_source": "live_tick_log",
        "clean_a2_labelable_episode_count": 99,
    }))

    with pytest.raises(store.DenseAccrualStoreError, match="provenance_mismatch"):
        store.load_dense_summary(legacy)
    with pytest.raises(store.DenseAccrualStoreError, match="summary_missing"):
        store.load_dense_summary(tmp_path / "missing.json")
