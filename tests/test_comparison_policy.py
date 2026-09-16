"""Numerical policy tests are isolated; no test scores are exported."""
from copy import deepcopy

from quality.config import METRICS
from quality.validation import compare


def report(rate):
    metrics = {name: {"pass_rate": rate, "unknown": 0, "pending": 0} for name in METRICS}
    return {"metrics": metrics, "held_out": {"metrics": deepcopy(metrics)}}


def test_any_quality_metric_can_supply_required_improvement():
    baseline, candidate = report(.8), report(.8)
    candidate["metrics"]["topic_relevance"]["pass_rate"] = .85
    assert compare(baseline, candidate)["passed"]
    candidate["metrics"]["task_completion"]["pass_rate"] = .77
    assert not compare(baseline, candidate)["passed"]


def test_holdout_regression_blocks_otherwise_improved_candidate():
    baseline, candidate = report(.8), report(.9)
    candidate["held_out"]["metrics"]["task_completion"]["pass_rate"] = .74
    assert not compare(baseline, candidate)["passed"]
