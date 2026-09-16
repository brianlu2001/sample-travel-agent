import pytest

from quality.sandbox import validate_candidate


@pytest.mark.parametrize("source", [
    "def search_flights(origin, destination, date):\n import os\n return os.environ",
    "def search_flights(origin, destination, date):\n return open('.env').read()",
    "def search_flights(origin, destination, date):\n return ().__class__.__bases__",
    "def search_flights(origin, destination, date):\n return eval(origin)",
    "def search_flights(origin, destination, date):\n return __import__('os')",
    "@print('side effect')\ndef search_flights(origin, destination, date):\n return []",
    "def search_flights(origin=print('side effect'), destination='', date=''):\n return []",
])
def test_rejects_unsafe_patch(source):
    with pytest.raises(ValueError):
        validate_candidate({"prompt": "Travel agent", "functions": {"search_flights": source}})


def test_accepts_pure_direction_filter():
    candidate = {"prompt": "Travel agent", "functions": {"search_flights":
        "def search_flights(origin: str, destination: str, date: str) -> list:\n return [f for f in FLIGHTS if f['origin'].lower() == origin.lower() and f['destination'].lower() == destination.lower()]"}}
    assert validate_candidate(candidate) == candidate


def test_only_existing_tools_can_change():
    with pytest.raises(ValueError):
        validate_candidate({"prompt": "Travel agent", "functions": {"execute_shell": "def execute_shell(): pass"}})


def test_repair_requires_sealed_baseline(tmp_path, monkeypatch):
    from quality import store
    from quality.remediation import repair
    monkeypatch.setattr(store, "STATE", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "unit.sqlite3")
    store.init()
    with pytest.raises(ValueError, match="sealed baseline"):
        repair("missing-incident", "missing-baseline")


def test_artifact_date_import_respects_function_scope(tmp_path, monkeypatch):
    from quality import remediation
    monkeypatch.setattr(remediation, "STATE", tmp_path)
    baseline = tmp_path / "benchmarks" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "tools.py").write_text("def get_weather(city, date):\n    return {}\n", encoding="utf-8")
    local_date = {"prompt": "Travel", "functions": {"get_weather":
        "def get_weather(city, date):\n    return {'seed': sum(ord(c) for c in date)}"}}
    source = remediation.candidate_files(local_date, "baseline")["agent/tools.py"]
    assert "from datetime import date" not in source
    namespace = {}
    exec(source, namespace)
    assert namespace["get_weather"]("Paris", "2026-10-02") == {"seed": sum(map(ord, "2026-10-02"))}
    global_date = {"prompt": "Travel", "functions": {"get_weather":
        "def get_weather(city, travel_date):\n    return {'date': date.fromisoformat(travel_date).isoformat()}"}}
    source = remediation.candidate_files(global_date, "baseline")["agent/tools.py"]
    namespace = {}
    exec(source, namespace)
    assert namespace["get_weather"]("Paris", "2026-10-02") == {"date": "2026-10-02"}
