"""Public Alibaba Cloud Function Compute API for Sizzle.

Cloudflare forwards same-origin `/api/*` requests here with a bearer token.
This function validates requests, persists job state in OSS, and asynchronously
invokes the pipeline Function Compute function. The browser never sees Alibaba
credentials or the Qwen Cloud API key.

Function Compute entrypoint: ``api.handler`` (Python HTTP trigger).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import fc2
import oss2
from oss2.credentials import EnvironmentVariableCredentialsProvider

RUN_ID = re.compile(r"^run_[a-f0-9]{24}$")


def _bucket() -> oss2.Bucket:
    auth = oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())
    return oss2.Bucket(
        auth,
        os.environ["OSS_ENDPOINT"],
        os.environ["OSS_BUCKET"],
        region=os.environ["ALIBABA_CLOUD_REGION"],
    )


def _response(start_response, body: dict, status: str = "200 OK"):
    start_response(
        status,
        [
            ("content-type", "application/json; charset=utf-8"),
            ("cache-control", "no-store"),
            ("x-content-type-options", "nosniff"),
        ],
    )
    return [json.dumps(body).encode()]


def _authorized(environ: dict) -> bool:
    expected = os.environ.get("EDGE_API_TOKEN", "")
    supplied = environ.get("HTTP_AUTHORIZATION", "").removeprefix("Bearer ")
    return bool(expected) and hmac.compare_digest(expected, supplied)


def _valid_url(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("URLs must use HTTPS and contain no credentials")
    return value


def _job_key(run_id: str) -> str:
    return f"runs/{run_id}/job.json"


def _load_job(run_id: str) -> dict:
    return json.loads(_bucket().get_object(_job_key(run_id)).read())


def _save_job(job: dict) -> None:
    _bucket().put_object(
        _job_key(job["run_id"]),
        json.dumps(job, separators=(",", ":")),
        headers={"content-type": "application/json"},
    )


def _invoke_pipeline(job: dict) -> None:
    client = fc2.Client(
        endpoint=os.environ.get("SIZZLE_FC_ENDPOINT", os.environ.get("FC_ENDPOINT", "")),
        accessKeyID=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
        accessKeySecret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
        securityToken=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN"),
    )
    client.invoke_function(
        os.environ.get("SIZZLE_FC_SERVICE_NAME", os.environ.get("FC_SERVICE_NAME", "")),
        os.environ.get("SIZZLE_FC_PIPELINE_FUNCTION", os.environ.get("FC_PIPELINE_FUNCTION", "")),
        payload=json.dumps({"run_id": job["run_id"]}).encode(),
        headers={
            "x-fc-invocation-type": "Async",
            "x-fc-async-task-id": job["run_id"],
        },
    )


def _create_run(environ: dict, start_response):
    try:
        length = min(int(environ.get("CONTENT_LENGTH") or 0), 16_384)
        payload = json.loads(environ["wsgi.input"].read(length))
        repo_url = _valid_url(payload.get("repo_url"), required=True)
        app_url = _valid_url(payload.get("app_url"), required=False)
    except (ValueError, TypeError, json.JSONDecodeError, KeyError) as exc:
        return _response(start_response, {"error": "invalid_request", "message": str(exc)}, "400 Bad Request")

    run_id = f"run_{uuid.uuid4().hex[:24]}"
    job = {
        "run_id": run_id,
        "status": "queued",
        "repo_url": repo_url,
        "app_url": app_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_job(job)
    _invoke_pipeline(job)
    return _response(start_response, {"run_id": run_id, "status": "queued"}, "202 Accepted")


def _get_run(run_id: str, start_response):
    if not RUN_ID.fullmatch(run_id):
        return _response(start_response, {"error": "not_found"}, "404 Not Found")
    try:
        job = _load_job(run_id)
    except oss2.exceptions.NoSuchKey:
        return _response(start_response, {"error": "not_found"}, "404 Not Found")

    result = {key: value for key, value in job.items() if key not in {"repo_url", "app_url", "error_detail"}}
    if job.get("final_cut_key"):
        result["final_cut_url"] = _bucket().sign_url("GET", job["final_cut_key"], 900)
    return _response(start_response, result)


def handler(environ, start_response):
    """HTTP trigger handler used by the Cloudflare edge Worker."""
    if not _authorized(environ):
        return _response(start_response, {"error": "unauthorized"}, "401 Unauthorized")

    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if method == "GET" and path == "/api/health":
        return _response(
            start_response,
            {
                "status": "ok",
                "edge": "cloudflare",
                "backend": "alibaba-cloud-function-compute",
                "inference": "qwen-cloud",
            },
        )
    if method == "POST" and path == "/api/runs":
        return _create_run(environ, start_response)
    if method == "GET" and path.startswith("/api/runs/"):
        return _get_run(path.rsplit("/", 1)[-1], start_response)
    return _response(start_response, {"error": "not_found"}, "404 Not Found")
