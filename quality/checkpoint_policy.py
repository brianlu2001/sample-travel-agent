"""Checkpoint decisions over paired scenarios. Historical gates remain historical."""
import random
from statistics import mean

from quality.config import PRIMARY_METRICS, ROOT, file_hash, fingerprint

POLICY = {"name": "daily-checkpoints-v2", "interval_seconds": 86400,
          "targeted_cases": 15, "targeted_repetitions": 1, "max_revisions": 3,
          "material_delta": .02, "minimum_pairs": 10, "bootstrap_samples": 4000,
          "family_alpha": .05, "comparison_slots": 12, "metrics": PRIMARY_METRICS}


def version():
    return fingerprint({"policy": POLICY, "code": file_hash([ROOT / "quality" / "checkpoint_policy.py"])})


def _groups(rows, name):
    groups = {}
    for row in rows:
        label = (row.get("evaluation") or {}).get("metrics", {}).get(name, {}).get("label", "pending")
        groups.setdefault(row["scenario_id"], []).append(label)
    return groups


def compare(before_rows, after_rows):
    """Bootstrap whole scenarios so repeated generations are not independent samples.

    Intervals are exploratory benchmark uncertainty, not calibrated guarantees.
    A conservative family adjustment covers 3 metrics × 2 cohorts × 2 comparators.
    """
    metrics = {}
    for name in PRIMARY_METRICS:
        before, after = _groups(before_rows, name), _groups(after_rows, name)
        differences = []
        excluded, applicability_changed = [], []
        for identifier in sorted(set(before) | set(after)):
            a, b = before.get(identifier, []), after.get(identifier, [])
            if tuple(a.count(x) for x in ("not_applicable", "unknown", "pending")) != tuple(b.count(x) for x in ("not_applicable", "unknown", "pending")):
                applicability_changed.append(identifier)
            if not a or not b or any(x not in ("pass", "fail") for x in a+b) or len(a) != len(b):
                excluded.append(identifier)
                continue
            differences.append(mean(x == "pass" for x in b) - mean(x == "pass" for x in a))
        def rate(groups):
            labels = [label for labels in groups.values() for label in labels if label in ("pass", "fail")]
            return labels.count("pass") / len(labels) if labels else None
        old, new = rate(before), rate(after)
        delta = new-old if old is not None and new is not None else None
        interval, conclusion = None, "inconclusive"
        if len(differences) >= POLICY["minimum_pairs"]:
            rng = random.Random(0)
            samples = sorted(mean(rng.choices(differences, k=len(differences))) for _ in range(POLICY["bootstrap_samples"]))
            tail = POLICY["family_alpha"] / POLICY["comparison_slots"] / 2
            interval = [samples[int(tail*len(samples))], samples[min(len(samples)-1, int((1-tail)*len(samples)))]]
            if interval[1] < -POLICY["material_delta"]:
                conclusion = "regressed"
            elif interval[0] > POLICY["material_delta"] and not applicability_changed:
                conclusion = "improved"
            elif interval[0] >= -POLICY["material_delta"] and not applicability_changed:
                conclusion = "stable"
        metrics[name] = {"before": old, "after": new, "delta": delta,
                         "observed_decline": delta is not None and delta < -1e-12,
                         "paired_scenarios": len(differences), "paired_delta": mean(differences) if differences else None,
                         "interval": interval, "excluded_scenarios": excluded,
                         "applicability_changed": applicability_changed, "conclusion": conclusion}
    conclusions = [m["conclusion"] for m in metrics.values()]
    conclusion = "regressed" if "regressed" in conclusions else "improved" if "improved" in conclusions and "inconclusive" not in conclusions else "inconclusive"
    return {"conclusion": conclusion, "metrics": metrics,
            "interpretation": "Scenario-cluster bootstrap with a conservative family adjustment. This describes this benchmark, not judge accuracy or guaranteed population performance."}
