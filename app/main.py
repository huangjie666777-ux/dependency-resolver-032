from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import resolver

app = FastAPI(title="Offline dependency resolver", version="1.0.0")


@app.exception_handler(resolver.InputError)
async def input_error_handler(request: Request, exc: resolver.InputError):
    return JSONResponse(
        status_code=422,
        content={"status": "invalid_input", "field": exc.field, "message": exc.message},
    )


def _reason(source_kind: str, locked: bool, constraints) -> str:
    if locked:
        return "version pinned by lock and compatible with all other constraints"
    kinds = {c["source_kind"] for c in constraints}
    if kinds == {"root"}:
        return "highest catalog version satisfying the root requirement"
    return "highest catalog version satisfying all incoming constraints"


@app.post("/resolve")
async def resolve(payload: dict):
    if not isinstance(payload, dict):
        raise resolver.InputError("body", "request body must be a JSON object")
    catalog = resolver.parse_catalog(payload.get("catalog"))
    requirements = resolver.parse_requirements(payload.get("requirements"), catalog)
    locks = resolver.parse_locks(payload.get("locks"), catalog)

    result = resolver.solve(catalog, requirements, locks)
    if not result.ok:
        mus, conflicts = resolver.conflict_report(catalog, requirements, locks)
        return {
            "status": "unsatisfiable",
            "minimal_unsatisfiable_subset": mus,
            "conflicts": conflicts,
            "resolved": [],
            "edges": [],
        }

    locked_names = {lock.name for lock in locks}
    resolved = []
    edges = []
    for name in sorted(result.selected):
        version = result.selected[name]
        entries = result.applied.get(name, [])
        constraints = [
            {
                "range": ac.range_text,
                "source_kind": ac.source_kind,
                "source": ac.source,
            }
            for ac in entries
        ]
        resolved.append(
            {
                "name": name,
                "version": resolver.format_version(version),
                "constraints": constraints,
                "reason": _reason(None, name in locked_names, constraints),
            }
        )
        pv = catalog.packages[name][version]
        for dep in pv.dependencies:
            edges.append(
                {
                    "from": name,
                    "from_version": resolver.format_version(version),
                    "to": dep.name,
                    "to_version": resolver.format_version(result.selected[dep.name]),
                    "range": dep.range_text,
                }
            )
    edges.sort(key=lambda e: (e["from"], e["to"], e["range"]))
    return {"status": "resolved", "resolved": resolved, "edges": edges}


@app.get("/health")
async def health():
    return {"status": "ok"}
