from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from engine import config, logs
from engine.parser import PipelineValidationError, parse_pipeline_yaml
from registry import auth, db, metadata
from registry.init_db import create_schema
from registry.metadata import DuplicateArtifactError
from registry.storage import ChecksumMismatchError, load_blob, save_blob


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(config.db_path())
    create_schema()
    yield


app = FastAPI(title="Forge", version="0.1.0", lifespan=lifespan)


def require_write_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    identity = auth.verify_token(token)
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

    run_id = str(uuid.uuid4())
    metadata.create_run(run_id=run_id, pipeline_name=parsed.name, pipeline_yaml=raw.decode("utf-8"))
    return {"run_id": run_id}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, object]:
    run = metadata.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "id": run.id,
        "pipeline_name": run.pipeline_name,
        "status": run.status,
        "lockfile_url": run.lockfile_url,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "duration_s": run.duration_s,
    }


@app.get("/runs/{run_id}/lockfile")
def get_lockfile(run_id: str) -> dict[str, object]:
    run = metadata.get_run(run_id)
    if run is None or run.lockfile is None:
        raise HTTPException(status_code=404, detail="lockfile not found")
    return run.lockfile


@app.get("/runs/{run_id}/logs")
def get_logs(run_id: str, follow: bool = False) -> StreamingResponse:
    if metadata.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return StreamingResponse(
        logs.stream_run_logs(run_id, follow=follow),
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
    import io
    publisher = require_write_token(authorization)
    blob_data = await file.read()
    try:
        sha256 = save_blob(io.BytesIO(blob_data), checksum)
        size = len(blob_data)
        metadata.put_artifact(name=name, version=version, sha256=sha256, size=size, publisher=publisher)
    except ChecksumMismatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DuplicateArtifactError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"name": name, "version": version, "sha256": sha256, "size": size}, status_code=201)


@app.get("/artifacts/{name}/{version}")
def download_artifact(name: str, version: str) -> StreamingResponse:
    artifact = metadata.get_artifact(name, version)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return StreamingResponse(
        load_blob(artifact.sha256),
        media_type="application/octet-stream",
        headers={"X-Artifact-SHA256": artifact.sha256},
    )


@app.get("/artifacts/{name}/{version}/meta")
def artifact_meta(name: str, version: str) -> dict[str, object]:
    artifact = metadata.get_artifact(name, version)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {
        "name": artifact.name,
        "version": artifact.version,
        "sha256": artifact.sha256,
        "size": artifact.size,
        "publisher": artifact.publisher,
        "published_at": artifact.published_at,
    }


@app.get("/artifacts/{name}")
def list_artifact_versions(name: str) -> dict[str, list[str]]:
    return {"versions": metadata.list_versions(name)}
