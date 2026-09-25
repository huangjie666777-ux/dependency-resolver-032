"""Offline dependency resolution core.

Pure-Python, deterministic backtracking resolver. No I/O here so it can be
unit tested directly and reused by the HTTP layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_PACKAGES = 15
MAX_VERSIONS_PER_PACKAGE = 8

OPS = (">=", "<=", "==", ">", "<")


class InputError(Exception):
    """Raised for malformed input. 'field' points at the offending field."""

    def __init__(self, field: str, message: str):
        super().__init__(field + ": " + message)
        self.field = field
        self.message = message


def parse_version(text, field: str) -> tuple:
    if not isinstance(text, str):
        raise InputError(field, "version must be a string like '1.2.3'")
    parts = text.split(".")
    if len(parts) != 3:
        raise InputError(field, "version %r must have exactly three numeric segments" % text)
    nums = []
    for part in parts:
        if not part.isdigit():
            raise InputError(field, "version %r segments must be non-negative integers" % text)
        nums.append(int(part))
    return tuple(nums)


def format_version(version) -> str:
    return ".".join(str(n) for n in version)


@dataclass(frozen=True)
class Constraint:
    op: str
    version: tuple

    def matches(self, version) -> bool:
        if self.op == "==":
            return version == self.version
        if self.op == ">=":
            return version >= self.version
        if self.op == ">":
            return version > self.version
        if self.op == "<=":
            return version <= self.version
        if self.op == "<":
            return version < self.version
        raise AssertionError(self.op)

    def __str__(self) -> str:
        return self.op + format_version(self.version)


def parse_range(text, field: str) -> tuple:
    if not isinstance(text, str) or not text.strip():
        raise InputError(field, "range must be a non-empty string")
    constraints = []
    for raw in text.split(","):
        token = raw.strip()
        if not token:
            raise InputError(field, "range %r contains an empty constraint" % text)
        op = None
        rest = token
        for candidate in OPS:
            if token.startswith(candidate):
                op = candidate
                rest = token[len(candidate):].strip()
                break
        if op is None:
            op = "=="  # bare version means exact match
        version = parse_version(rest, field)
        constraints.append(Constraint(op, version))
    return tuple(constraints)


@dataclass(frozen=True)
class Dependency:
    name: str
    constraints: tuple
    range_text: str


@dataclass(frozen=True)
class PackageVersion:
    version: tuple
    dependencies: tuple


@dataclass
class Catalog:
    packages: dict  # name -> {version_tuple: PackageVersion}


def parse_catalog(raw, field: str = "catalog") -> Catalog:
    if not isinstance(raw, list):
        raise InputError(field, "catalog must be an array of packages")
    if len(raw) > MAX_PACKAGES:
        raise InputError(field, "catalog supports at most %d packages" % MAX_PACKAGES)
    packages = {}
    for i, entry in enumerate(raw):
        pfield = "%s[%d]" % (field, i)
        if not isinstance(entry, dict):
            raise InputError(pfield, "package entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise InputError(pfield + ".name", "package name must be a non-empty string")
        if name in packages:
            raise InputError(pfield + ".name", "duplicate package name %r" % name)
        raw_versions = entry.get("versions")
        if not isinstance(raw_versions, list) or not raw_versions:
            raise InputError(pfield + ".versions", "versions must be a non-empty array")
        if len(raw_versions) > MAX_VERSIONS_PER_PACKAGE:
            raise InputError(
                pfield + ".versions",
                "a package supports at most %d versions" % MAX_VERSIONS_PER_PACKAGE,
            )
        versions = {}
        for j, ventry in enumerate(raw_versions):
            vfield = "%s.versions[%d]" % (pfield, j)
            if not isinstance(ventry, dict):
                raise InputError(vfield, "version entry must be an object")
            version = parse_version(ventry.get("version"), vfield + ".version")
            if version in versions:
                raise InputError(
                    vfield + ".version",
                    "duplicate version %r for package %r" % (format_version(version), name),
                )
            raw_deps = ventry.get("dependencies", [])
            if not isinstance(raw_deps, list):
                raise InputError(vfield + ".dependencies", "dependencies must be an array")
            deps = []
            for k, dentry in enumerate(raw_deps):
                dfield = "%s.dependencies[%d]" % (vfield, k)
                if not isinstance(dentry, dict):
                    raise InputError(dfield, "dependency must be an object")
                dep_name = dentry.get("name")
                if not isinstance(dep_name, str) or not dep_name:
                    raise InputError(dfield + ".name", "dependency name must be a non-empty string")
                constraints = parse_range(dentry.get("range"), dfield + ".range")
                deps.append(Dependency(dep_name, constraints, dentry.get("range")))
            versions[version] = PackageVersion(version, tuple(deps))
        packages[name] = versions
    for name, versions in packages.items():
        for version, pv in versions.items():
            for dep in pv.dependencies:
                if dep.name not in packages:
                    raise InputError(
                        "catalog",
                        "package %r %s depends on unknown package %r"
                        % (name, format_version(version), dep.name),
                    )
    return Catalog(packages)


@dataclass(frozen=True)
class Requirement:
    name: str
    constraints: tuple
    range_text: str
    label: str


def parse_requirements(raw, catalog: Catalog, field: str = "requirements") -> tuple:
    if not isinstance(raw, list) or not raw:
        raise InputError(field, "requirements must be a non-empty array")
    reqs = []
    for i, entry in enumerate(raw):
        rfield = "%s[%d]" % (field, i)
        if not isinstance(entry, dict):
            raise InputError(rfield, "requirement must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise InputError(rfield + ".name", "requirement name must be a non-empty string")
        if name not in catalog.packages:
            raise InputError(rfield + ".name", "unknown package %r" % name)
        constraints = parse_range(entry.get("range"), rfield + ".range")
        reqs.append(Requirement(name, constraints, entry.get("range"), "root requirement #%d" % (i + 1)))
    return tuple(reqs)


@dataclass(frozen=True)
class Lock:
    name: str
    version: tuple
    label: str


def parse_locks(raw, catalog: Catalog, field: str = "locks") -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InputError(field, "locks must be an array")
    locks = []
    seen = set()
    for i, entry in enumerate(raw):
        lfield = "%s[%d]" % (field, i)
        if not isinstance(entry, dict):
            raise InputError(lfield, "lock entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise InputError(lfield + ".name", "lock name must be a non-empty string")
        if name in seen:
            raise InputError(lfield + ".name", "duplicate lock for package %r" % name)
        seen.add(name)
        if name not in catalog.packages:
            raise InputError(lfield + ".name", "unknown package %r" % name)
        version = parse_version(entry.get("version"), lfield + ".version")
        if version not in catalog.packages[name]:
            raise InputError(
                lfield + ".version",
                "package %r has no version %r in the catalog" % (name, format_version(version)),
            )
        locks.append(Lock(name, version, "lock #%d" % (i + 1)))
    return tuple(locks)


@dataclass
class AppliedConstraint:
    """A constraint applied to a package, with provenance for explanations."""

    constraints: tuple
    range_text: str
    source_kind: str  # "root" | "dependency" | "lock"
    source: str
    chain: tuple  # package chain from a root to the constrained package


@dataclass
class SolveResult:
    ok: bool
    selected: dict = field(default_factory=dict)
    applied: dict = field(default_factory=dict)


def _satisfies(version, constraints) -> bool:
    return all(c.matches(version) for c in constraints)


def solve(catalog: Catalog, requirements: tuple, locks: tuple) -> SolveResult:
    """Backtracking search. Deterministic: packages are processed in sorted
    name order and versions are tried from highest to lowest."""

    lock_by_name = {lock.name: lock for lock in locks}

    def lock_constraint(lock):
        return AppliedConstraint(
            (Constraint("==", lock.version),),
            "==" + format_version(lock.version),
            "lock",
            lock.label,
            (lock.name,),
        )

    def seed():
        applied = {}
        for req in requirements:
            applied.setdefault(req.name, []).append(
                AppliedConstraint(req.constraints, req.range_text, "root", req.label, (req.name,))
            )
        # locks only constrain packages that turn out to be needed
        for name in list(applied):
            if name in lock_by_name:
                applied[name].append(lock_constraint(lock_by_name[name]))
        return applied

    def search(selected, applied):
        pending = sorted(name for name in applied if name not in selected)
        if not pending:
            return SolveResult(True, dict(selected), applied)
        name = pending[0]
        constraints = applied[name]
        versions = sorted(catalog.packages[name], reverse=True)
        for version in versions:
            if not all(_satisfies(version, ac.constraints) for ac in constraints):
                continue
            pv = catalog.packages[name][version]
            next_applied = {k: list(v) for k, v in applied.items()}
            conflict = False
            for dep in pv.dependencies:
                ac = AppliedConstraint(
                    dep.constraints,
                    dep.range_text,
                    "dependency",
                    "%s %s" % (name, format_version(version)),
                    (name, dep.name),
                )
                if dep.name in selected and not _satisfies(selected[dep.name], dep.constraints):
                    conflict = True
                    break
                bucket = next_applied.setdefault(dep.name, [])
                if dep.name not in applied and dep.name in lock_by_name:
                    bucket.append(lock_constraint(lock_by_name[dep.name]))
                bucket.append(ac)
            if conflict:
                continue
            selected[name] = version
            result = search(selected, next_applied)
            if result.ok:
                return result
            del selected[name]
        return SolveResult(False)

    return search({}, seed())


def find_mus(catalog: Catalog, requirements: tuple, locks: tuple) -> list:
    """Deletion-based minimal unsatisfiable subset over root requirements and
    locks. Removing any remaining item makes the subset solvable."""

    items = [("requirement", r) for r in requirements] + [("lock", l) for l in locks]

    def solvable(subset) -> bool:
        reqs = tuple(item for kind, item in subset if kind == "requirement")
        lks = tuple(item for kind, item in subset if kind == "lock")
        return solve(catalog, reqs, lks).ok

    subset = list(items)
    changed = True
    while changed:
        changed = False
        for entry in list(subset):
            trial = [e for e in subset if e is not entry]
            if not solvable(trial):
                subset = trial
                changed = True
    return subset


def conflict_report(catalog: Catalog, requirements, locks):
    """Build the unsat explanation: MUS plus conflicted packages and chains."""
    mus = find_mus(catalog, requirements, locks)
    mus_labels = []
    for kind, item in mus:
        if kind == "requirement":
            mus_labels.append(
                {"kind": "root", "name": item.name, "range": item.range_text, "label": item.label}
            )
        else:
            mus_labels.append(
                {
                    "kind": "lock",
                    "name": item.name,
                    "version": format_version(item.version),
                    "label": item.label,
                }
            )

    reqs = tuple(item for kind, item in mus if kind == "requirement")
    lks = tuple(item for kind, item in mus if kind == "lock")
    lock_by_name = {lock.name: lock for lock in lks}

    def lock_ac(lock):
        return AppliedConstraint(
            (Constraint("==", lock.version),),
            "==" + format_version(lock.version),
            "lock",
            lock.label,
            (lock.name,),
        )

    applied = {}
    for req in reqs:
        applied.setdefault(req.name, []).append(
            AppliedConstraint(req.constraints, req.range_text, "root", req.label, (req.name,))
        )
    for name in list(applied):
        if name in lock_by_name:
            applied[name].append(lock_ac(lock_by_name[name]))
    # propagate dependency constraints for every catalog version so chains
    # through the conflict are visible regardless of which version is picked
    queue = list(applied)
    seen_edges = set()
    while queue:
        name = queue.pop(0)
        for version, pv in sorted(catalog.packages[name].items()):
            for dep in pv.dependencies:
                edge_key = (name, version, dep.name)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                if dep.name not in applied and dep.name in lock_by_name:
                    applied.setdefault(dep.name, []).append(lock_ac(lock_by_name[dep.name]))
                applied.setdefault(dep.name, []).append(
                    AppliedConstraint(
                        dep.constraints,
                        dep.range_text,
                        "dependency",
                        "%s %s" % (name, format_version(version)),
                        (name, dep.name),
                    )
                )
                if dep.name not in queue:
                    queue.append(dep.name)

    conflicts = []
    for name in sorted(applied):
        entries = applied[name]
        available = sorted(catalog.packages[name])
        surviving = [v for v in available if all(_satisfies(v, ac.constraints) for ac in entries)]
        if surviving:
            continue
        conflicts.append(
            {
                "package": name,
                "available_versions": [format_version(v) for v in available],
                "constraints": [
                    {
                        "range": ac.range_text,
                        "source_kind": ac.source_kind,
                        "source": ac.source,
                        "chain": list(ac.chain),
                    }
                    for ac in entries
                ],
            }
        )
    return mus_labels, conflicts
