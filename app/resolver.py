"""Offline dependency resolution core.

Pure-python, deterministic backtracking resolver. No network access.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_PACKAGES = 15
MAX_VERSIONS_PER_PACKAGE = 8

OPS = (">=", "<=", ">", "<", "==")


class InputError(Exception):
    """Raised when the request payload is structurally invalid."""

    def __init__(self, errors: list[dict]):
        super().__init__("invalid input")
        self.errors = errors


@dataclass(frozen=True)
class Constraint:
    op: str
    version: tuple[int, int, int]
    source: str  # human readable origin, e.g. "root", "lock", "web@1.2.0"


def parse_version(text, field: str, errors: list[dict]):
    """Parse a 'X.Y.Z' version into a tuple of ints, or record an error."""
    if not isinstance(text, str):
        errors.append({"field": field, "message": "version must be a string like '1.2.3'"})
        return None
    parts = text.split(".")
    if len(parts) != 3 or any(not p.isdigit() for p in parts):
        errors.append(
            {"field": field, "message": f"invalid version '{text}': expected three dot-separated non-negative integers"}
        )
        return None
    return tuple(int(p) for p in parts)


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in version)


def parse_range(text, field: str, errors: list[dict]):
    """Parse a constraint range: comma separated exact / >= / > / <= / < terms."""
    if not isinstance(text, str) or not text.strip():
        errors.append({"field": field, "message": "range must be a non-empty string"})
        return None
    terms = []
    for raw in text.split(","):
        token = raw.strip()
        op = "=="
        body = token
        for candidate in OPS:
            if token.startswith(candidate):
                op = candidate
                body = token[len(candidate):].strip()
                break
        version = parse_version(body, field, errors)
        if version is None:
            return None
        terms.append((op, version))
    return terms


def matches(version: tuple[int, int, int], terms) -> bool:
    for op, target in terms:
        if op == "==" and version != target:
            return False
        if op == ">=" and version < target:
            return False
        if op == "<=" and version > target:
            return False
        if op == ">" and version <= target:
            return False
        if op == "<" and version >= target:
            return False
    return True


def matches_constraint(version, constraint: Constraint) -> bool:
    return matches(version, [(constraint.op, constraint.version)])


@dataclass
class PackageVersion:
    version: tuple[int, int, int]
    dependencies: list  # list of (name, terms, range_text)


@dataclass
class Catalog:
    # name -> {version_tuple: PackageVersion}
    packages: dict

    def versions_desc(self, name: str):
        return sorted(self.packages[name], reverse=True)


def build_catalog(catalog_input) -> Catalog:
    errors: list[dict] = []
    packages: dict[str, dict] = {}
    if not isinstance(catalog_input, list) or not catalog_input:
        raise InputError([{"field": "catalog", "message": "catalog must be a non-empty array"}])
    if len(catalog_input) > MAX_PACKAGES:
        raise InputError([{"field": "catalog", "message": f"catalog supports at most {MAX_PACKAGES} packages"}])

    for i, entry in enumerate(catalog_input):
        field = f"catalog[{i}]"
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            errors.append({"field": f"{field}.name", "message": "package name must be a non-empty string"})
            continue
        if name in packages:
            errors.append({"field": f"{field}.name", "message": f"duplicate package name '{name}'"})
            continue
        versions_input = entry.get("versions")
        if not isinstance(versions_input, list) or not versions_input:
            errors.append({"field": f"{field}.versions", "message": "versions must be a non-empty array"})
            continue
        if len(versions_input) > MAX_VERSIONS_PER_PACKAGE:
            errors.append(
                {"field": f"{field}.versions", "message": f"package '{name}' has more than {MAX_VERSIONS_PER_PACKAGE} versions"}
            )
            continue
        versions: dict[tuple, PackageVersion] = {}
        for j, ver_entry in enumerate(versions_input):
            vfield = f"{field}.versions[{j}]"
            ver_text = ver_entry.get("version") if isinstance(ver_entry, dict) else None
            version = parse_version(ver_text, f"{vfield}.version", errors)
            if version is None:
                continue
            if version in versions:
                errors.append(
                    {"field": f"{vfield}.version", "message": f"duplicate version '{ver_text}' for package '{name}'"}
                )
                continue
            deps_input = ver_entry.get("dependencies", [])
            if not isinstance(deps_input, list):
                errors.append({"field": f"{vfield}.dependencies", "message": "dependencies must be an array"})
                continue
            deps = []
            for k, dep in enumerate(deps_input):
                dfield = f"{vfield}.dependencies[{k}]"
                dep_name = dep.get("name") if isinstance(dep, dict) else None
                if not isinstance(dep_name, str) or not dep_name:
                    errors.append({"field": f"{dfield}.name", "message": "dependency name must be a non-empty string"})
                    continue
                terms = parse_range(dep.get("range"), f"{dfield}.range", errors)
                if terms is None:
                    continue
                deps.append((dep_name, terms, dep.get("range")))
            versions[version] = PackageVersion(version=version, dependencies=deps)
        packages[name] = versions

    if errors:
        raise InputError(errors)

    # Unknown references are checked once every package name is known.
    for i, entry in enumerate(catalog_input):
        for j, ver_entry in enumerate(entry["versions"]):
            for k, dep in enumerate(ver_entry.get("dependencies", [])):
                dep_name = dep.get("name")
                if isinstance(dep_name, str) and dep_name and dep_name not in packages:
                    errors.append(
                        {
                            "field": f"catalog[{i}].versions[{j}].dependencies[{k}].name",
                            "message": f"unknown package reference '{dep_name}'",
                        }
                    )
    if errors:
        raise InputError(errors)
    return Catalog(packages=packages)


def parse_requirements(requirements_input, catalog: Catalog) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(requirements_input, list) or not requirements_input:
        raise InputError([{"field": "requirements", "message": "requirements must be a non-empty array"}])
    parsed = []
    seen = set()
    for i, req in enumerate(requirements_input):
        field = f"requirements[{i}]"
        name = req.get("name") if isinstance(req, dict) else None
        if not isinstance(name, str) or not name:
            errors.append({"field": f"{field}.name", "message": "requirement name must be a non-empty string"})
            continue
        if name not in catalog.packages:
            errors.append({"field": f"{field}.name", "message": f"unknown package reference '{name}'"})
            continue
        if name in seen:
            errors.append({"field": f"{field}.name", "message": f"duplicate root requirement for '{name}'"})
            continue
        seen.add(name)
        terms = parse_range(req.get("range"), f"{field}.range", errors)
        if terms is None:
            continue
        parsed.append({"name": name, "terms": terms, "range": req.get("range")})
    if errors:
        raise InputError(errors)
    return parsed


def parse_locks(locks_input, catalog: Catalog) -> list[dict]:
    if locks_input is None:
        return []
    errors: list[dict] = []
    if not isinstance(locks_input, list):
        raise InputError([{"field": "locks", "message": "locks must be an array"}])
    parsed = []
    seen = set()
    for i, lock in enumerate(locks_input):
        field = f"locks[{i}]"
        name = lock.get("name") if isinstance(lock, dict) else None
        if not isinstance(name, str) or not name:
            errors.append({"field": f"{field}.name", "message": "lock name must be a non-empty string"})
            continue
        if name not in catalog.packages:
            errors.append({"field": f"{field}.name", "message": f"unknown package reference '{name}'"})
            continue
        if name in seen:
            errors.append({"field": f"{field}.name", "message": f"duplicate lock for '{name}'"})
            continue
        seen.add(name)
        version = parse_version(lock.get("version"), f"{field}.version", errors)
        if version is None:
            continue
        if version not in catalog.packages[name]:
            errors.append(
                {
                    "field": f"{field}.version",
                    "message": f"locked version '{lock.get('version')}' of '{name}' is not in the catalog",
                }
            )
            continue
        parsed.append({"name": name, "version": version})
    if errors:
        raise InputError(errors)
    return parsed


def solve(catalog: Catalog, requirements: list[dict], locks: list[dict]):
    """Deterministic backtracking search.

    Returns (assignment, constraints) or None. Only packages reachable from
    the roots are ever assigned, so unrelated catalog entries never appear.
    Deterministic: packages are tried in name order, versions descending.
    """
    base_constraints: dict[str, list[Constraint]] = {}
    for req in requirements:
        for op, ver in req["terms"]:
            base_constraints.setdefault(req["name"], []).append(Constraint(op, ver, "root"))
    for lock in locks:
        base_constraints.setdefault(lock["name"], []).append(Constraint("==", lock["version"], "lock"))

    assignment: dict[str, tuple] = {}
    constraints: dict[str, list[Constraint]] = {k: list(v) for k, v in base_constraints.items()}
    needed: set[str] = {req["name"] for req in requirements}

    def search() -> bool:
        pending = sorted(name for name in needed if name not in assignment)
        if not pending:
            return True
        name = pending[0]
        for version in catalog.versions_desc(name):
            if not all(matches_constraint(version, c) for c in constraints.get(name, [])):
                continue
            deps = catalog.packages[name][version].dependencies
            if any(
                dep_name in assignment and not matches(assignment[dep_name], terms)
                for dep_name, terms, _ in deps
            ):
                continue
            assignment[name] = version
            added_needed = []
            added_constraints = []
            for dep_name, terms, _ in deps:
                if dep_name not in needed:
                    needed.add(dep_name)
                    added_needed.append(dep_name)
                for op, ver in terms:
                    constraint = Constraint(op, ver, f"{name}@{format_version(version)}")
                    constraints.setdefault(dep_name, []).append(constraint)
                    added_constraints.append((dep_name, constraint))
            if search():
                return True
            for dep_name, constraint in added_constraints:
                constraints[dep_name].remove(constraint)
            for dep_name in added_needed:
                needed.discard(dep_name)
            del assignment[name]
        return False

    if search():
        return assignment, constraints
    return None


def _constraint_report(constraints, assignment) -> dict:
    report = {}
    for name in sorted(assignment):
        entries = []
        for c in constraints.get(name, []):
            kind = c.source if c.source in ("root", "lock") else "dependency"
            entries.append({"type": kind, "source": c.source, "constraint": f"{c.op}{format_version(c.version)}"})
        entries.sort(key=lambda e: (e["type"] != "root", e["type"] != "lock", e["source"], e["constraint"]))
        report[name] = entries
    return report


def resolve(payload: dict) -> dict:
    """Full resolution pipeline. Raises InputError or returns a result dict."""
    if not isinstance(payload, dict):
        raise InputError([{"field": "$", "message": "request body must be a JSON object"}])
    for key in ("catalog", "requirements"):
        if key not in payload:
            raise InputError([{"field": key, "message": "missing required field"}])
    catalog = build_catalog(payload["catalog"])
    requirements = parse_requirements(payload["requirements"], catalog)
    locks = parse_locks(payload.get("locks"), catalog)

    solved = solve(catalog, requirements, locks)
    if solved is None:
        return _unsat_report(catalog, requirements, locks)
    assignment, constraints = solved

    resolved = [{"name": name, "version": format_version(assignment[name])} for name in sorted(assignment)]
    edges = []
    for name in sorted(assignment):
        version = assignment[name]
        for dep_name, _, range_text in sorted(catalog.packages[name][version].dependencies):
            edges.append({"from": name, "to": dep_name, "range": range_text})
    constraint_report = _constraint_report(constraints, assignment)
    packages = []
    for name in sorted(assignment):
        applied = constraint_report[name]
        packages.append(
            {
                "name": name,
                "version": format_version(assignment[name]),
                "constraints": applied,
                "reason": (
                    f"selected {format_version(assignment[name])}: highest catalog version that satisfies "
                    + ("; ".join(f"{c['source']} {c['constraint']}" for c in applied) or "no constraints")
                ),
            }
        )
    return {"status": "resolved", "resolved": resolved, "edges": edges, "packages": packages}


def _minimal_core(catalog: Catalog, requirements: list[dict], locks: list[dict]):
    """Irreducible unsatisfiable subset of root requirements + locks.

    Removing any single remaining item makes the set satisfiable.
    """
    items = [("requirement", req) for req in requirements] + [("lock", lock) for lock in locks]
    kept = list(items)

    def satisfiable(without_index=None) -> bool:
        reqs = [item for i, (kind, item) in enumerate(kept) if kind == "requirement" and i != without_index]
        lks = [item for i, (kind, item) in enumerate(kept) if kind == "lock" and i != without_index]
        if not reqs:
            return True  # no roots -> nothing to install, trivially satisfiable
        return solve(catalog, reqs, lks) is not None

    changed = True
    while changed:
        changed = False
        for i in range(len(kept)):
            if not satisfiable(without_index=i):
                kept.pop(i)
                changed = True
                break
    return kept


def _dependency_chains(catalog: Catalog, core_items) -> list[dict]:
    """Shortest catalog-level dependency chains between packages named in the core."""
    core_packages = sorted({item["name"] for _, item in core_items})
    graph: dict[str, set[str]] = {name: set() for name in catalog.packages}
    for name, versions in catalog.packages.items():
        for pv in versions.values():
            for dep_name, _, _ in pv.dependencies:
                graph[name].add(dep_name)
    chains = []
    for src in core_packages:
        prev = {src: None}
        queue = [src]
        while queue:
            node = queue.pop(0)
            for nxt in sorted(graph.get(node, ())):
                if nxt not in prev:
                    prev[nxt] = node
                    queue.append(nxt)
        for dst in core_packages:
            if dst == src or dst not in prev:
                continue
            path = [dst]
            while path[-1] != src:
                path.append(prev[path[-1]])
            path.reverse()
            if len(path) > 1:
                chains.append({"from": src, "to": dst, "chain": path})
    chains.sort(key=lambda c: (c["from"], c["to"]))
    return chains


def _unsat_report(catalog: Catalog, requirements: list[dict], locks: list[dict]) -> dict:
    core = _minimal_core(catalog, requirements, locks)
    core_items = []
    for kind, item in core:
        if kind == "requirement":
            core_items.append({"type": "requirement", "name": item["name"], "range": item["range"]})
        else:
            core_items.append({"type": "lock", "name": item["name"], "version": format_version(item["version"])})
    conflicting = sorted({item["name"] for item in core_items})
    return {
        "status": "unsatisfiable",
        "message": "no combination of catalog versions satisfies every root requirement and lock",
        "minimal_unsatisfiable_subset": core_items,
        "conflicting_packages": conflicting,
        "dependency_chains": _dependency_chains(catalog, core),
    }
