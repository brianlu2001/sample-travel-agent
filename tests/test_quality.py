"""Isolated unit checks: never export test doubles to telemetry or benchmark stores."""
import json

import pytest

from quality import store
from quality.monitoring import summarize
from quality.privacy import redact_text, safe_payload
from quality.profiles.travel import reference_tool, validate_tools


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "unit.sqlite3")
    store.init()


def test_reference_direction_and_missing_dates():
    result = reference_tool("search_flights", {"origin": "New York", "destination": "Miami", "date": "2026-10-02"})
    assert {f["flight_number"] for f in result["matches"]} == {"DL 883", "B6 1029"}
    assert "date-specific" in result["limitation"]


def test_reference_full_hotel_stay_and_invalid_dates():
    assert not reference_tool("search_hotels", {"city": "New York", "check_in": "2026-12-31", "check_out": "2027-01-05"})["matches"]
    assert "error" in reference_tool("search_hotels", {"city": "Paris", "check_in": "2026-06-14", "check_out": "2026-06-10"})


def test_tool_validator_detects_contract_violations():
    # Deliberate invalid values are isolated unit fixtures, never telemetry.
    inputs = [("search_flights", dict(origin="New York", destination="Miami", date="2026-10-02"), [{"flight_number": "UNKNOWN"}]),
              ("search_hotels", dict(city="New York", check_in="2026-12-31", check_out="2027-01-05"), [{"name": "invalid"}]),
              ("create_itinerary", dict(destination="Paris", num_days=3), {"days": [{"day": 1}]}),
              ("get_weather", dict(city="Miami", date="2026-10-02"), {"high_f": -999, "low_f": -999})]
    diagnostics, _ = validate_tools([{"name": n, "arguments": a, "result": result} for n, a, result in inputs])
    assert all(d["label"] == "fail" for d in diagnostics)


def test_missing_evaluations_do_not_become_passes():
    result = summarize([{"evaluation": {"metrics": {"correctness": {"label": "pass"}}}},
                        {"evaluation": {"metrics": {"correctness": {"label": "unknown"}}}},
                        {"evaluation": None}])["metrics"]["correctness"]
    assert result["n"] == 1 and result["pass_rate"] == 1
    assert result["unknown"] == 1 and result["pending"] == 1
    assert result["coverage"] == pytest.approx(1/3)


def test_idempotent_queue_and_lease(isolated_store):
    store.enqueue("unit", "once", {"ok": True})
    store.enqueue("unit", "once", {"ok": True})
    first = store.claim(("unit",))
    assert first and store.claim(("unit",)) is None
    store.fail(first, ValueError("not persisted"))
    row = store.rows("SELECT * FROM jobs")[0]
    assert row["error"] == "ValueError" and "not persisted" not in json.dumps(row)
    store.execute("UPDATE jobs SET available=0")
    retried = store.claim(("unit",))
    assert retried["attempts"] == 2
    store.finish(retried)
    assert store.claim(("unit",)) is None


def test_redaction_preserves_travel_constraints():
    text = "My name is Jane Smith. Email jane@example.com or call 212-555-0198. Plan 3 days in Paris under $200."
    result = redact_text(text)
    assert all(secret not in result for secret in ("Jane Smith", "jane@example.com", "212-555-0198"))
    assert "Paris" in result and "$200" in result and "3 days" in result


def test_redaction_failure_withholds_payload(monkeypatch):
    from quality import privacy
    def broken():
        raise RuntimeError("NLP unavailable")
    monkeypatch.setattr(privacy, "analyzer", broken)
    assert safe_payload("jane@example.com") == {"privacy_error": "redaction_unavailable", "content": "[WITHHELD]"}


@pytest.mark.parametrize("text,secret,retained", [
    ("My passport number is X12345678. Find flights to Miami.", "X12345678", "Find flights to Miami."),
    ("My address is 123 Maple Street. Plan a two-day trip to Chicago, one line per day.", "123 Maple Street", "Plan a two-day trip to Chicago, one line per day."),
    ("My name is Austin. Find flights to Austin.", "My name is Austin", "Find flights to Austin."),
    ("Convert a $200 budget into euros.", "[PERSON]", "Convert a $200 budget into euros."),
])
def test_privacy_boundary_regressions(text, secret, retained):
    redacted = redact_text(text)
    assert secret not in redacted
    assert retained in redacted
