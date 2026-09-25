import copy
import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text())


def post(payload):
    return client.post("/resolve", json=payload)


# ---------- invalid input ----------

def test_duplicate_package_name():
    payload = load("success.json")
    payload["catalog"].append(copy.deepcopy(payload["catalog"][0]))
    resp = post(payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "invalid_input"
    assert "duplicate package name" in body["message"]
    assert body["field"].startswith("catalog[")


def test_duplicate_version():
    payload = load("success.json")
    pkg = payload["catalog"][2]  # util
    pkg["versions"].append(copy.deepcopy(pkg["versions"][0]))
    resp = post(payload)
    assert resp.status_code == 422
    assert "duplicate version" in resp.json()["message"]


def test_unknown_dependency_reference():
    payload = load("success.json")
    payload["catalog"][0]["versions"][0]["dependencies"].append(
        {"name": "ghost", "range": ">=1.0.0"}
    )
    resp = post(payload)
    assert resp.status_code == 422
    assert "unknown package" in resp.json()["message"]


def test_unknown_requirement():
    payload = load("success.json")
    payload["requirements"] = [{"name": "ghost", "range": ">=1.0.0"}]
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["field"] == "requirements[0].name"


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4", "1.2.x", "-1.0.0", 123, None])
def test_illegal_version(bad):
    payload = load("success.json")
    payload["catalog"][2]["versions"][0]["version"] = bad
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["field"].endswith(".version")


@pytest.mark.parametrize("bad", ["", "   ", ">=", "=>1.0.0", ">=1.0.0,", ",>=1.0.0", 5])
def test_illegal_range(bad):
    payload = load("success.json")
    payload["requirements"] = [{"name": "web", "range": bad}]
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["field"] == "requirements[0].range"


def test_limits():
    payload = {
        "catalog": [
            {"name": "p%02d" % i, "versions": [{"version": "1.0.0", "dependencies": []}]}
            for i in range(16)
        ],
        "requirements": [{"name": "p00", "range": ">=1.0.0"}],
    }
    assert post(payload).status_code == 422
    payload = {
        "catalog": [
            {
                "name": "p",
                "versions": [
                    {"version": "1.0.%d" % i, "dependencies": []} for i in range(9)
                ],
            }
        ],
        "requirements": [{"name": "p", "range": ">=1.0.0"}],
    }
    assert post(payload).status_code == 422


def test_lock_unknown_version():
    payload = load("success.json")
    payload["locks"] = [{"name": "util", "version": "9.9.9"}]
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["field"] == "locks[0].version"


# ---------- resolution ----------

def test_success_with_lock():
    resp = post(load("success.json"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    picked = {p["name"]: p["version"] for p in body["resolved"]}
    assert picked == {"web": "3.0.0", "db": "2.0.0", "util": "1.2.0"}
    # edges reflect the actual dependency graph
    edge_pairs = {(e["from"], e["to"]) for e in body["edges"]}
    assert edge_pairs == {("web", "db"), ("web", "util"), ("db", "util")}
    # util carries root-of-chain, dependency and lock constraints
    util = next(p for p in body["resolved"] if p["name"] == "util")
    kinds = {c["source_kind"] for c in util["constraints"]}
    assert kinds == {"dependency", "lock"}
    assert "lock" in util["reason"]
    # no unrelated packages installed
    assert len(body["resolved"]) == 3


def test_backtrack_to_lower_version():
    body = post(load("backtrack.json")).json()
    assert body["status"] == "resolved"
    picked = {p["name"]: p["version"] for p in body["resolved"]}
    # lib 2.1.0 needs core>=9.0.0 which does not exist; must fall back to 2.0.0
    assert picked == {"app": "1.0.0", "lib": "2.0.0", "core": "1.5.0"}


def test_compatible_cycle():
    body = post(load("cycle.json")).json()
    assert body["status"] == "resolved"
    picked = {p["name"]: p["version"] for p in body["resolved"]}
    assert picked == {"a": "1.0.0", "b": "1.0.0"}
    edge_pairs = {(e["from"], e["to"]) for e in body["edges"]}
    assert edge_pairs == {("a", "b"), ("b", "a")}


def test_lock_conflict_unsat():
    body = post(load("lock-conflict.json")).json()
    assert body["status"] == "unsatisfiable"
    assert body["resolved"] == []
    mus = body["minimal_unsatisfiable_subset"]
    assert len(mus) == 2
    kinds = sorted(item["kind"] for item in mus)
    assert kinds == ["lock", "root"]
    conflict_pkgs = {c["package"] for c in body["conflicts"]}
    assert "db" in conflict_pkgs
    db = next(c for c in body["conflicts"] if c["package"] == "db")
    chains = [c["chain"] for c in db["constraints"]]
    assert ["web", "db"] in chains
    assert ["db"] in chains  # lock chain


def test_mus_minimality():
    # three roots, only two of them conflict; MUS must drop the unrelated one
    payload = load("lock-conflict.json")
    payload["catalog"].append(
        {"name": "extra", "versions": [{"version": "1.0.0", "dependencies": []}]}
    )
    payload["requirements"].append({"name": "extra", "range": ">=1.0.0"})
    body = post(payload).json()
    assert body["status"] == "unsatisfiable"
    mus = body["minimal_unsatisfiable_subset"]
    assert len(mus) == 2
    assert all(item["name"] != "extra" for item in mus)


def test_locks_do_not_install_unrelated_packages():
    payload = load("success.json")
    payload["catalog"].append(
        {"name": "unused", "versions": [{"version": "1.0.0", "dependencies": []}]}
    )
    payload["locks"].append({"name": "unused", "version": "1.0.0"})
    body = post(payload).json()
    assert body["status"] == "resolved"
    names = {p["name"] for p in body["resolved"]}
    assert "unused" not in names


def test_determinism_under_shuffle():
    payload = load("success.json")
    baseline = post(payload).json()
    rng = random.Random(42)
    for _ in range(10):
        shuffled = copy.deepcopy(payload)
        rng.shuffle(shuffled["catalog"])
        for pkg in shuffled["catalog"]:
            rng.shuffle(pkg["versions"])
            for v in pkg["versions"]:
                rng.shuffle(v["dependencies"])
        rng.shuffle(shuffled["requirements"])
        rng.shuffle(shuffled["locks"])
        assert post(shuffled).json() == baseline


def test_range_operators():
    payload = {
        "catalog": [
            {
                "name": "p",
                "versions": [
                    {"version": v, "dependencies": []}
                    for v in ["1.0.0", "1.5.0", "2.0.0", "2.5.0"]
                ],
            }
        ],
        "requirements": [{"name": "p", "range": ">1.0.0,<=2.0.0"}],
    }
    body = post(payload).json()
    assert body["resolved"][0]["version"] == "2.0.0"
    payload["requirements"] = [{"name": "p", "range": "1.5.0"}]
    body = post(payload).json()
    assert body["resolved"][0]["version"] == "1.5.0"
