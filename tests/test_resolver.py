import copy
import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.resolver import InputError, resolve

client = TestClient(app)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text())


def pkg(name, versions):
    return {"name": name, "versions": versions}


def ver(v, deps=None):
    return {"version": v, "dependencies": deps or []}


def test_backtracking_finds_feasible_lower_version():
    result = resolve(load("backtrack.json"))
    assert result["status"] == "resolved"
    resolved = {p["name"]: p["version"] for p in result["resolved"]}
    # db 2.1.0 requires driver 9.9.9 which does not exist -> must fall back to db 2.0.0
    assert resolved == {"web": "2.0.0", "db": "2.0.0", "driver": "1.2.0"}
    # unrelated catalog package must not be installed
    assert "unrelated" not in resolved
    assert {"from": "web", "to": "db", "range": ">=2.0.0"} in result["edges"]
    assert {"from": "db", "to": "driver", "range": ">=1.0.0"} in result["edges"]


def test_compatible_cycle_resolves():
    result = resolve(load("cycle.json"))
    assert result["status"] == "resolved"
    resolved = {p["name"]: p["version"] for p in result["resolved"]}
    assert resolved == {"alpha": "1.2.0", "beta": "2.1.0"}
    assert len(result["edges"]) == 2


def test_lock_conflict_reports_minimal_core():
    payload = load("lock_conflict.json")
    response = client.post("/resolve", json=payload)
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "unsatisfiable"
    core = body["minimal_unsatisfiable_subset"]
    assert {"type": "requirement", "name": "app", "range": "1.0.0"} in core
    assert {"type": "lock", "name": "lib", "version": "1.0.0"} in core
    assert body["conflicting_packages"] == ["app", "lib"]
    assert any(c["chain"] == ["app", "lib"] for c in body["dependency_chains"])
    # no partial manifest leaks into a conflict response
    assert "resolved" not in body


def test_locks_do_not_pull_unrelated_packages():
    payload = load("backtrack.json")
    payload["locks"] = [{"name": "unrelated", "version": "3.0.0"}]
    result = resolve(payload)
    assert result["status"] == "resolved"
    assert "unrelated" not in {p["name"] for p in result["resolved"]}


def test_lock_pins_version():
    payload = load("backtrack.json")
    payload["locks"] = [{"name": "db", "version": "1.5.0"}]
    result = resolve(payload)
    resolved = {p["name"]: p["version"] for p in result["resolved"]}
    # db locked to 1.5.0 -> web 2.0.0 impossible -> web falls back to 1.0.0
    assert resolved == {"web": "1.0.0", "db": "1.5.0"}
    db_pkg = next(p for p in result["packages"] if p["name"] == "db")
    assert any(c["type"] == "lock" for c in db_pkg["constraints"])


def test_input_order_does_not_change_result():
    payload = load("backtrack.json")
    baseline = resolve(payload)
    for seed in range(10):
        shuffled = copy.deepcopy(payload)
        rng = random.Random(seed)
        rng.shuffle(shuffled["catalog"])
        for entry in shuffled["catalog"]:
            rng.shuffle(entry["versions"])
            for v in entry["versions"]:
                rng.shuffle(v["dependencies"])
        assert resolve(shuffled) == baseline


def test_shared_dependency_satisfies_all_sources():
    payload = {
        "catalog": [
            pkg("a", [ver("1.0.0", [{"name": "shared", "range": ">=1.0.0,<2.0.0"}])]),
            pkg("b", [ver("1.0.0", [{"name": "shared", "range": ">=1.2.0"}])]),
            pkg("shared", [ver("1.1.0"), ver("1.3.0"), ver("2.0.0")]),
        ],
        "requirements": [{"name": "a", "range": "1.0.0"}, {"name": "b", "range": "1.0.0"}],
    }
    result = resolve(payload)
    resolved = {p["name"]: p["version"] for p in result["resolved"]}
    # shared 2.0.0 violates a's <2.0.0, 1.1.0 violates b's >=1.2.0 -> 1.3.0
    assert resolved["shared"] == "1.3.0"
    shared_pkg = next(p for p in result["packages"] if p["name"] == "shared")
    sources = {c["source"] for c in shared_pkg["constraints"]}
    assert sources == {"a@1.0.0", "b@1.0.0"}


def test_duplicate_package_name_rejected_with_field():
    payload = {
        "catalog": [pkg("a", [ver("1.0.0")]), pkg("a", [ver("2.0.0")])],
        "requirements": [{"name": "a", "range": ">=1.0.0"}],
    }
    response = client.post("/resolve", json=payload)
    assert response.status_code == 422
    errors = response.json()["errors"]
    assert any(e["field"] == "catalog[1].name" and "duplicate" in e["message"] for e in errors)


def test_duplicate_version_rejected():
    payload = {
        "catalog": [pkg("a", [ver("1.0.0"), ver("1.0.0")])],
        "requirements": [{"name": "a", "range": ">=1.0.0"}],
    }
    with pytest.raises(InputError) as exc:
        resolve(payload)
    assert any("catalog[0].versions[1].version" == e["field"] for e in exc.value.errors)


def test_unknown_reference_rejected():
    payload = {
        "catalog": [pkg("a", [ver("1.0.0", [{"name": "ghost", "range": ">=1.0.0"}])])],
        "requirements": [{"name": "a", "range": ">=1.0.0"}],
    }
    response = client.post("/resolve", json=payload)
    assert response.status_code == 422
    assert "ghost" in response.json()["errors"][0]["message"]


def test_invalid_range_rejected_with_field():
    payload = {
        "catalog": [pkg("a", [ver("1.0.0")])],
        "requirements": [{"name": "a", "range": ">=1.0"}],
    }
    response = client.post("/resolve", json=payload)
    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "requirements[0].range"


def test_numeric_version_comparison():
    payload = {
        "catalog": [pkg("a", [ver("1.9.0"), ver("1.10.0")])],
        "requirements": [{"name": "a", "range": ">1.9.0"}],
    }
    result = resolve(payload)
    assert result["resolved"] == [{"name": "a", "version": "1.10.0"}]


def test_limits_enforced():
    payload = {
        "catalog": [pkg(f"p{i}", [ver("1.0.0")]) for i in range(16)],
        "requirements": [{"name": "p0", "range": ">=1.0.0"}],
    }
    with pytest.raises(InputError):
        resolve(payload)
    payload = {
        "catalog": [pkg("a", [ver(f"1.0.{i}") for i in range(9)])],
        "requirements": [{"name": "a", "range": ">=1.0.0"}],
    }
    with pytest.raises(InputError):
        resolve(payload)


def test_unsat_not_confused_with_local_conflict():
    # Highest versions clash locally, but a feasible combination exists.
    payload = {
        "catalog": [
            pkg("a", [ver("2.0.0", [{"name": "c", "range": "<1.0.0"}]), ver("1.0.0", [{"name": "c", "range": ">=1.0.0"}])]),
            pkg("b", [ver("1.0.0", [{"name": "c", "range": ">=1.0.0"}])]),
            pkg("c", [ver("1.0.0")]),
        ],
        "requirements": [{"name": "a", "range": ">=1.0.0"}, {"name": "b", "range": "1.0.0"}],
    }
    result = resolve(payload)
    assert result["status"] == "resolved"
    resolved = {p["name"]: p["version"] for p in result["resolved"]}
    assert resolved == {"a": "1.0.0", "b": "1.0.0", "c": "1.0.0"}


def test_truly_unsat_core_is_minimal():
    payload = {
        "catalog": [
            pkg("a", [ver("1.0.0", [{"name": "c", "range": "<1.0.0"}])]),
            pkg("b", [ver("1.0.0", [{"name": "c", "range": ">=1.0.0"}])]),
            pkg("c", [ver("0.9.0"), ver("1.0.0")]),
            pkg("ok", [ver("1.0.0")]),
        ],
        "requirements": [
            {"name": "a", "range": "1.0.0"},
            {"name": "b", "range": "1.0.0"},
            {"name": "ok", "range": "1.0.0"},
        ],
    }
    result = resolve(payload)
    assert result["status"] == "unsatisfiable"
    core_names = sorted(item["name"] for item in result["minimal_unsatisfiable_subset"])
    # 'ok' is unrelated to the conflict and must be dropped from the core
    assert core_names == ["a", "b"]
