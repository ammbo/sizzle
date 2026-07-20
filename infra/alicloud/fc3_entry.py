"""FC 3.0 Python runtime entry point for the Sizzle HTTP API.

FC 3.0 HTTP trigger format: handler(event: bytes, context) -> response_dict

This thin wrapper parses the FC 3.0 HTTP event format and delegates
to the core API logic.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import base64
import email.utils
import hashlib
import urllib.request
import urllib.error

import oss2
from oss2.credentials import EnvironmentVariableCredentialsProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sizzle.api")

RUN_ID = re.compile(r"^run_[a-f0-9]{24}$")


def _bucket() -> oss2.Bucket:
    auth = oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())
    return oss2.Bucket(
        auth,
        os.environ["OSS_ENDPOINT"],
        os.environ["OSS_BUCKET"],
        region=os.environ.get("ALIBABA_CLOUD_REGION", "ap-southeast-1"),
    )


def _response(body: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(body),
    }


def _authorized(headers: dict) -> bool:
    expected = os.environ.get("EDGE_API_TOKEN", "")
    auth_header = headers.get("authorization", headers.get("Authorization", ""))
    supplied = auth_header.removeprefix("Bearer ")
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


def _invoke_fc3(function_name: str, payload: bytes, extra_headers: dict | None = None) -> None:
    """Invoke an FC 3.0 function via the Alibaba Cloud OpenAPI (ROA V1 signing)."""
    ak_id = os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"]
    ak_secret = os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"]
    sts_token = os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN")
    endpoint = os.environ.get("SIZZLE_FC_ENDPOINT", "")

    path = f"/2023-03-30/functions/{function_name}/invocations"
    url = f"{endpoint}{path}"
    log.info("invoke_fc3 function=%s url=%s", function_name, url)

    content_type = "application/json"
    accept = "application/json"
    content_md5 = base64.b64encode(hashlib.md5(payload).digest()).decode()
    date = email.utils.formatdate(usegmt=True)

    acs_headers: dict[str, str] = {"x-acs-version": "2023-03-30"}
    if sts_token:
        acs_headers["x-acs-security-token"] = sts_token

    canonical = "".join(f"{k}:{acs_headers[k]}\n" for k in sorted(acs_headers))
    string_to_sign = f"POST\n{accept}\n{content_md5}\n{content_type}\n{date}\n{canonical}{path}"
    signature = base64.b64encode(
        hmac.new(ak_secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()

    headers = {
        "Accept": accept,
        "Content-Type": content_type,
        "Content-MD5": content_md5,
        "Date": date,
        "Authorization": f"acs {ak_id}:{signature}",
        **acs_headers,
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        log.info("invoke_fc3_ok function=%s status=%d", function_name, resp.status)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        log.error("invoke_fc3_failed function=%s status=%d body=%s", function_name, e.code, body)
        raise
    except Exception as e:
        log.error("invoke_fc3_error function=%s error=%s", function_name, e, exc_info=True)
        raise


def _invoke_pipeline(job: dict) -> None:
    _invoke_fc3(
        os.environ.get("SIZZLE_FC_PIPELINE_FUNCTION", ""),
        json.dumps({"run_id": job["run_id"]}).encode(),
        extra_headers={
            "x-fc-invocation-type": "Async",
            "x-fc-async-task-id": job["run_id"],
        },
    )


def handler(event: bytes, context) -> dict:
    """FC 3.0 HTTP trigger handler."""
    # Parse the HTTP trigger event
    try:
        req = json.loads(event) if event else {}
    except (json.JSONDecodeError, TypeError):
        req = {}

    headers = req.get("headers", {})
    method = req.get("httpMethod", req.get("requestContext", {}).get("http", {}).get("method", "GET"))
    path = req.get("rawPath", req.get("path", "/"))
    body_str = req.get("body", "")
    log.info("request method=%s path=%s", method, path)

    if not _authorized(headers):
        log.warning("unauthorized_request path=%s", path)
        return _response({"error": "unauthorized"}, 401)

    # Health check
    if method == "GET" and path == "/api/health":
        return _response({
            "status": "ok",
            "edge": "cloudflare",
            "backend": "alibaba-cloud-function-compute",
            "inference": "qwen-cloud",
        })

    # Create run
    if method == "POST" and path == "/api/runs":
        try:
            payload = json.loads(body_str) if body_str else {}
            repo_url = _valid_url(payload.get("repo_url"), required=True)
            app_url = _valid_url(payload.get("app_url"), required=False)
        except (ValueError, TypeError, json.JSONDecodeError, KeyError) as exc:
            log.warning("invalid_request error=%s", exc)
            return _response({"error": "invalid_request", "message": str(exc)}, 400)

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
        log.info("run_created run_id=%s repo_url=%s", run_id, repo_url)
        try:
            _invoke_pipeline(job)
        except Exception as exc:
            log.error("pipeline_invoke_failed run_id=%s error=%s", run_id, exc, exc_info=True)
            job.update({"status": "failed", "error": "invoke_failed", "error_detail": str(exc)[:500]})
            _save_job(job)
            return _response({"run_id": run_id, "status": "failed", "error": "Failed to start pipeline"}, 500)
        return _response({"run_id": run_id, "status": "queued"}, 202)

    # Get run status
    if method == "GET" and path.startswith("/api/runs/"):
        run_id = path.rsplit("/", 1)[-1]
        if not RUN_ID.fullmatch(run_id):
            return _response({"error": "not_found"}, 404)
        try:
            job = _load_job(run_id)
        except oss2.exceptions.NoSuchKey:
            return _response({"error": "not_found"}, 404)

        # Mark stale runs as timed out (FC pipeline timeout is 900s + 60s buffer)
        if job.get("status") in ("queued", "running"):
            updated = datetime.fromisoformat(job["updated_at"])
            if (datetime.now(timezone.utc) - updated).total_seconds() > 960:
                job.update({
                    "status": "failed",
                    "error": "timeout",
                    "error_detail": "Run exceeded the maximum pipeline duration.",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                _save_job(job)

        result = {k: v for k, v in job.items() if k not in {"repo_url", "app_url", "error_detail"}}
        if job.get("cf_video_key"):
            result["video_url"] = f"/runs/{run_id}/video.mp4"
            result["run_url"] = f"/runs/{run_id}"
        elif job.get("final_cut_key"):
            result["final_cut_url"] = _bucket().sign_url("GET", job["final_cut_key"], 900)
        return _response(result)

    return _response({"error": "not_found"}, 404)
