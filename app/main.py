from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.resolver import InputError, resolve

app = FastAPI(title="Offline dependency resolver", version="0.1.0")


@app.exception_handler(InputError)
async def input_error_handler(_request: Request, exc: InputError):
    return JSONResponse(status_code=422, content={"status": "invalid", "errors": exc.errors})


@app.post("/resolve")
async def resolve_endpoint(payload: dict):
    result = resolve(payload)
    if result["status"] == "unsatisfiable":
        return JSONResponse(status_code=409, content=result)
    return result
