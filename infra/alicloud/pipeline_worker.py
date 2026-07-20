"""Alibaba Cloud Function Compute worker that runs the complete Sizzle pipeline.

Deploy this as a custom-container FC function with asynchronous tasks enabled.
The container includes ffmpeg and Chromium; Qwen/HappyHorse/CosyVoice inference
is always performed through the Qwen Cloud international DashScope endpoint.

Function Compute entrypoint: ``pipeline_worker.handler``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

import oss2
from oss2.credentials import EnvironmentVariableCredentialsProvider

from sizzle.config import Config
from sizzle.pipeline import make_demo_video

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sizzle.pipeline_worker")


def _bucket() -> oss2.Bucket:
    auth = oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())
    return oss2.Bucket(
        auth,
        os.environ["OSS_ENDPOINT"],
        os.environ["OSS_BUCKET"],
        region=os.environ["ALIBABA_CLOUD_REGION"],
    )


def _job_key(run_id: str) -> str:
    return f"runs/{run_id}/job.json"


def _load_job(run_id: str) -> dict:
    return json.loads(_bucket().get_object(_job_key(run_id)).read())


def _save_job(job: dict) -> None:
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    _bucket().put_object(
        _job_key(job["run_id"]),
        json.dumps(job, separators=(",", ":")),
        headers={"content-type": "application/json"},
    )


def handler(event: bytes, context) -> str:
    payload = json.loads(event)
    run_id = payload["run_id"]
    log.info("pipeline_start run_id=%s", run_id)

    job = _load_job(run_id)
    job["status"] = "running"
    _save_job(job)
    log.info("job_loaded run_id=%s repo_url=%s app_url=%s", run_id, job.get("repo_url"), job.get("app_url"))

    try:
        with tempfile.TemporaryDirectory(prefix=f"{run_id}_") as tmp:
            log.info("work_dir=%s", tmp)
            cfg = Config(
                work_dir=Path(tmp),
                api_key=os.environ["DASHSCOPE_API_KEY"],
            )
            log.info("github_app_id=%s github_key_len=%d", cfg.github_app_id, len(cfg.github_app_private_key))

            manifest = make_demo_video(
                cfg,
                job["repo_url"],
                job.get("app_url"),
            )
            log.info("pipeline_done run_id=%s duration=%.1fs tokens=%d", run_id, manifest.final_duration_s, manifest.cost.total_tokens)

            run_dir = Path(manifest.final_cut).parent

            prefix = f"runs/{run_id}/artifacts"
            cut_key = f"{prefix}/final_cut.mp4"
            manifest_key = f"{prefix}/manifest.json"
            beat_sheet_key = f"{prefix}/beat_sheet.json"
            _bucket().put_object_from_file(cut_key, manifest.final_cut)
            _bucket().put_object_from_file(manifest_key, str(run_dir / "manifest.json"))
            _bucket().put_object_from_file(beat_sheet_key, str(run_dir / "beat_sheet.json"))
            log.info("oss_upload_done run_id=%s", run_id)

            cf_video_key = None
            try:
                from sizzle.r2_upload import r2_configured, upload_file, video_key

                r2_ok = r2_configured()
                log.info("r2_configured=%s bucket=%s", r2_ok, os.environ.get("R2_BUCKET_NAME", ""))
                if r2_ok:
                    cf_video_key = upload_file(
                        manifest.final_cut,
                        video_key(run_id),
                        content_type="video/mp4",
                    )
                    log.info("r2_upload_done run_id=%s key=%s", run_id, cf_video_key)
            except Exception as exc:
                log.warning("r2_upload_failed run_id=%s error=%s", run_id, exc, exc_info=True)

            job.update(
                {
                    "status": "completed",
                    "final_cut_key": cut_key,
                    "manifest_key": manifest_key,
                    "beat_sheet_key": beat_sheet_key,
                    "duration_s": manifest.final_duration_s,
                    "total_tokens": manifest.cost.total_tokens,
                    "critic_scores": manifest.critic_scores,
                }
            )
            if cf_video_key:
                job["cf_video_key"] = cf_video_key
            _save_job(job)
            log.info("run_completed run_id=%s", run_id)
    except Exception as exc:
        log.error("pipeline_failed run_id=%s error=%s", run_id, exc, exc_info=True)
        job.update(
            {
                "status": "failed",
                "error": "pipeline_failed",
                "error_detail": str(exc)[:1000],
            }
        )
        _save_job(job)
        raise

    return json.dumps({"run_id": run_id, "status": "completed"})
