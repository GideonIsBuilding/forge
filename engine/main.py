from __future__ import annotations

import uuid
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from engine import config, logs, slack
from engine import runs as run_lifecycle
from engine.runner import DockerRunner
from engine.scheduler import execute_pipeline
from engine.publisher import PublishError
from engine.preflight import run_preflight, PreflightError
from engine.deps import DepChecksumMismatchError, DepNotFoundError
from engine.parser import PipelineValidationError, parse_pipeline_yaml
from registry import auth, db, metadata
from registry.init_db import create_schema
from registry.metadata import DuplicateArtifactError
from registry.storage import ChecksumMismatchError, load_blob, save_blob

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load()
    db.init(config.db_path())
    create_schema()
    logger.info("Forge engine started on %s:%d", config.engine_host(), config.engine_port())
    yield
    logger.info("Forge engine shutting down")


app = FastAPI(title="Forge", version="0.1.0", lifespan=lifespan)


def require_write_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    identity = auth.verify_token(token)
    if identity is None:
        raise HTTPException(status_code=403, detail="invalid bearer token")
    return identity


async def _execute_pipeline_background(
    run_id: str,
    pipeline_yaml: str,
    forge_url: str,
    forge_token: str,
) -> None:
    from engine.parser import parse_pipeline_yaml as _parse

    try:
        pipeline = _parse(pipeline_yaml)
    except PipelineValidationError as exc:
        run_lifecycle.mark_run_status(run_id, "failed")
        logger.error("Run %s: re-parse failed unexpectedly: %s", run_id, exc)
        return

    try:
        run_preflight(pipeline, run_id)
    except PreflightError as exc:
        slack.notify_resolution_failure(
            run_id=run_id,
            pipeline_name=pipeline.name,
            detail=str(exc),
        )
        return

    # host_workspace: path Docker Desktop can mount (host-side)
    # container_workspace: path inside the API container (for publisher)
    workspace_root = Path(config.get("storage.host_workspace_dir", "/tmp/forge-workspaces"))
    

    workspace = workspace_root / run_id
    

    workspace.mkdir(parents=True, exist_ok=True)
    

    runner = DockerRunner()

    try:
        await execute_pipeline(
            pipeline=pipeline,
            run_id=run_id,
            runner=runner,
            workspace=workspace,
            forge_url=forge_url,
            forge_token=forge_token,
            max_concurrency=config.max_concurrency(),
        )
    except DepChecksumMismatchError as exc:
        logger.error("Run %s: integrity failure — %s", run_id, exc)
        run_lifecycle.mark_run_status(run_id, "integrity_failure")
        slack.notify_integrity_failure(
            run_id=run_id,
            artifact_name="unknown",
            version="unknown",
            expected_sha256=getattr(exc, "expected", "unknown"),
            actual_sha256=getattr(exc, "actual", "unknown"),
        )
    except DepNotFoundError as exc:
        logger.error("Run %s: dep not found — %s", run_id, exc)
        run_lifecycle.mark_run_status(run_id, "failed")
    except PublishError as exc:
        logger.error("Run %s: publish error — %s", run_id, exc)
        run_lifecycle.mark_run_status(run_id, "failed")
    except Exception:
        logger.exception("Run %s: unhandled error in pipeline execution", run_id)
        run_lifecycle.mark_run_status(run_id, "failed")


@app.post("/runs")
async def create_run(
    background_tasks: BackgroundTasks,
    pipeline: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    resolve_only: bool = False,
) -> dict[str, str]:
    identity = require_write_token(authorization)
    raw = await pipeline.read()
    pipeline_yaml = raw.decode("utf-8")
    try:
        parsed = parse_pipeline_yaml(raw.decode("utf-8"))
    except PipelineValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = str(uuid.uuid4())
    metadata.create_run(run_id=run_id, pipeline_name=parsed.name, pipeline_yaml=raw.decode("utf-8"))
    logger.info("Run %s queued by %s (pipeline=%s)", run_id, identity, parsed.name)

    if resolve_only:
        try:
            run_preflight(parsed, run_id)
        except PreflightError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"run_id": run_id}

    forge_url = config.registry_url()
    forge_token = authorization.removeprefix("Bearer ").strip()

    background_tasks.add_task(
        _execute_pipeline_background,
        run_id,
        pipeline_yaml,
        forge_url,
        forge_token,
    )

    return {"run_id": run_id}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, object]:
    run = metadata.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    jobs = metadata.list_jobs(run_id)
    return {
        "id": run.id,
        "pipeline_name": run.pipeline_name,
        "status": run.status,
        "lockfile_url": run.lockfile_url,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "duration_s": run.duration_s,
        "jobs": [
            {
                "name": j.name,
                "status": j.status,
                "needs": j.needs,
                "runtime": j.runtime,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "exit_code": j.exit_code,
            }
            for j in jobs
        ],
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
        metadata.put_artifact(
            name=name, version=version, sha256=sha256, size=size, publisher=publisher
        )
    except ChecksumMismatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DuplicateArtifactError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        {"name": name, "version": version, "sha256": sha256, "size": size}, status_code=201
    )


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
        "deps": artifact.deps,
    }


@app.get("/artifacts/{name}")
def list_artifact_versions(name: str) -> dict[str, list[str]]:
    return {"versions": metadata.list_versions(name)}
