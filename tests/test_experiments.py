"""Experiment plumbing: override merging, the walk-forward split and run-key deduplication."""

from quotelab.config import load_config
from quotelab.experiments.definitions import daily_jobs, event_study_jobs
from quotelab.experiments.jobs import Job, apply_overrides, job_key, walk_forward_pairs


def test_apply_overrides_merges_nested_keys_without_touching_the_base():
    base = {"engine": {"data_latency_ms": 5, "order_latency_ms": 10}, "fees": {"maker_bps": 2}}
    merged = apply_overrides(base, {"engine": {"order_latency_ms": 50}})
    assert merged["engine"] == {"data_latency_ms": 5, "order_latency_ms": 50}
    assert merged["fees"] == {"maker_bps": 2}
    assert base["engine"]["order_latency_ms"] == 10


def test_walk_forward_calibrates_on_the_previous_day_only():
    pairs = walk_forward_pairs(["2024-01-03", "2024-01-01", "2024-01-02"])
    assert pairs == [("2024-01-01", "2024-01-02"), ("2024-01-02", "2024-01-03")]


def test_jobs_that_reach_the_baseline_config_share_one_key():
    cfg = load_config()
    jobs = daily_jobs(cfg, "BTCUSDT", [("2024-01-14", "2024-01-15")])
    key_of = {(job.experiment, job.variant): job_key(job, cfg) for job in jobs}
    baseline = key_of[("E7", "baseline")]
    assert key_of[("E1", "queue_latency")] == baseline
    assert key_of[("E2", "glft")] == baseline
    assert key_of[("E4", "latency_10ms")] == baseline
    assert key_of[("E4", "latency_50ms")] != baseline
    assert key_of[("E2", "fixed")] == key_of[("E5", "fixed")]


def test_a_replay_window_changes_the_key():
    cfg = load_config()
    full_day = Job("E7", "baseline", "BTCUSDT", "2024-01-14", "2024-01-15")
    risk_off, _ = event_study_jobs("BTCUSDT", "2024-01-14", "2024-01-15", (10, 20))
    assert job_key(full_day, cfg) != job_key(risk_off, cfg)
