"""Fixed independent candidate gates. Generated code cannot edit these checks."""
import argparse
import json
from pathlib import Path

from quality.profiles.travel import reference_tool
from quality.sandbox import load_functions


def validate(path):
    candidate = json.loads(Path(path).read_text(encoding="utf-8"))
    tools = load_functions(candidate)
    checks = []
    for origin, destination in (("New York", "Miami"), ("Miami", "New York"), ("London", "Paris")):
        args = dict(origin=origin, destination=destination, date="2026-10-02")
        actual = tools["search_flights"](**args)
        expected = reference_tool("search_flights", args)["matches"]
        checks.append({"name": "flight_direction:"+origin+":"+destination,
                       "pass": {f["flight_number"] for f in actual} == {f["flight_number"] for f in expected}})
    for days in (1, 3, 5):
        result = tools["create_itinerary"]("Paris", days)
        checks.append({"name": "itinerary_days:"+str(days), "pass": [d["day"] for d in result["days"]] == list(range(1, days+1))})
    args = dict(city="New York", check_in="2026-12-31", check_out="2027-01-05")
    checks.append({"name": "full_hotel_stay", "pass": tools["search_hotels"](**args) == []})
    for city in ("Miami", "Tokyo", "Paris"):
        args = dict(city=city, date="2026-10-02")
        actual = tools["get_weather"](**args)
        expected = reference_tool("get_weather", args)
        checks.append({"name": "weather_fahrenheit:"+city, "pass": all(actual[k] == expected[k] for k in ("high_f", "low_f", "condition"))})
    return {"passed": all(c["pass"] for c in checks), "checks": checks}


def compare(baseline, candidate):
    reasons = []
    deltas = {}
    for name in ("correctness", "groundedness", "topic_relevance", "task_completion"):
        before, after = baseline["metrics"][name], candidate["metrics"][name]
        delta = (after["pass_rate"] or 0)-(before["pass_rate"] or 0)
        deltas[name] = delta
        if after["unknown"] > before["unknown"] or after["pending"]:
            reasons.append(name + ": evaluation coverage regressed")
        if delta < -0.02 - 1e-12:
            reasons.append(name + ": more than 2 percentage points regression")
        old_holdout, new_holdout = baseline["held_out"]["metrics"][name], candidate["held_out"]["metrics"][name]
        if (new_holdout["pass_rate"] or 0) < (old_holdout["pass_rate"] or 0)-0.05-1e-12:
            reasons.append(name + ": held-out regression exceeds 5 points")
    if max(deltas.values()) < 0.05-1e-12:
        reasons.append("No improvement of at least five points in any quality metric")
    return {"passed": not reasons, "reasons": reasons, "deltas": deltas,
            "interpretation": "Observed repeated benchmark results; not a claim of statistical significance or human-calibrated accuracy."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate")
    args = parser.parse_args()
    print(json.dumps(validate(args.candidate)))
