from __future__ import annotations

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from engine.logs import LogStore
from engine.parser import PipelineValidationError, parse_pipeline_yaml
from registry.auth import TokenStore
from registry.metadata import MetadataStore
from registry.storage import BlobStore

app = FastAPI(title="Forge", version="0.1.0")

metadata = MetadataStore("data/forge.db")
blobs = BlobStore("blobs")
logs = LogStore("logs")
tokens = TokenStore(metadata)


def require_write_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    identity = tokens.verify(token)
    if identity is None:
        raise HTTPException(status_code=403, detail="invalid bearer token")
    return identity


@app.post("/runs")
async def create_run(
    pipeline: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    require_write_token(authorization)
    raw = await pipeline.read()
    try:
        parsed = parse_pipeline_yaml(raw.decode("utf-8"))
    except PipelineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = metadata.create_run(parsed.name)
    metadata.update_run_status(run_id, "queued")
    return {"run_id": run_id}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, object]:
    run = metadata.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/runs/{run_id}/lockfile")
def get_lockfile(run_id: str) -> dict[str, object]:
    lockfile = metadata.get_lockfile(run_id)
    if lockfile is None:
        raise HTTPException(status_code=404, detail="lockfile not found")
    return lockfile


@app.get("/runs/{run_id}/logs")
def get_logs(run_id: str, follow: bool = False) -> StreamingResponse:
    return StreamingResponse(
        logs.stream(run_id, follow=follow),
        media_type="text/event-stream" if follow else "application/x-ndjson",
    )


@app.post("/artifacts/{name}/{version}", status_code=201)
async def publish_artifact(
    name: str,
    version: str,
    file: UploadFile = File(...),
    checksum: str = File(...),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    publisher = require_write_token(authorization)
    blob = await file.read()
    try:
        sha256, size = blobs.put(blob, expected_checksum=checksum)
        metadata.publish_artifact(name, version, sha256, size, publisher, deps=[])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except metadata.DuplicateArtifactError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"name": name, "version": version, "sha256": sha256, "size": size}, status_code=201)


@app.get("/artifacts/{name}/{version}")
def download_artifact(name: str, version: str) -> StreamingResponse:
    artifact = metadata.get_artifact(name, version)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return StreamingResponse(
        blobs.open_stream(artifact["sha256"]),
        media_type="application/octet-stream",
        headers={"X-Artifact-SHA256": artifact["sha256"]},
    )


@app.get("/artifacts/{name}/{version}/meta")
def artifact_meta(name: str, version: str) -> dict[str, object]:
    artifact = metadata.get_artifact(name, version)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return artifact


@app.get("/artifacts/{name}")
def list_artifact_versions(name: str) -> dict[str, list[str]]:
    return {"versions": metadata.list_versions(name)}
