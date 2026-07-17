"""Alibaba Cloud deployment proof (PRD §11): one file, all three services.

This is the render worker deployed to Alibaba Cloud Function Compute. Per render job it:
  1. pulls shot assets from Alibaba Cloud OSS,
  2. calls DashScope (Qwen Cloud, Singapore region) for any model work the job needs,
  3. runs the deterministic ffmpeg render,
  4. pushes the finished clip back to OSS.

Function Compute entrypoint: `handler`. Invoked by the orchestrator with one JSON
payload per shot, fanned out in parallel across shots.

Environment (set on the FC function):
  OSS_ENDPOINT      e.g. https://oss-ap-southeast-1.aliyuncs.com
  OSS_BUCKET        asset bucket for shots, audio, renders, manifests
  OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET
  DASHSCOPE_API_KEY Qwen Cloud API key (intl endpoint)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from http import HTTPStatus
from pathlib import Path

import dashscope
import oss2  # provided by the FC layer

dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
dashscope.api_key = os.environ["DASHSCOPE_API_KEY"]


def _bucket() -> oss2.Bucket:
    auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], os.environ["OSS_BUCKET"])


def _tighten_caption(text: str, max_words: int) -> str:
    """DashScope call on the render path: compress a caption to fit its plate."""
    rsp = dashscope.Generation.call(
        model="qwen3.7-max",
        messages=[
            {"role": "system", "content": f"Rewrite the caption in at most {max_words} words. Return only the caption."},
            {"role": "user", "content": text},
        ],
        result_format="message",
    )
    if rsp.status_code != HTTPStatus.OK:
        return text
    return rsp.output.choices[0].message.content.strip()


def _render(job: dict, workdir: Path) -> Path:
    """Deterministic ffmpeg render: still -> clip with push-in, house format."""
    still = workdir / "still.png"
    out = workdir / "clip.mp4"
    _bucket().get_object_to_file(job["still_key"], str(still))
    d, fps, (w, h) = job["duration_s"], job.get("fps", 24), job.get("size", (1280, 720))
    frames = max(int(d * fps), 1)
    vf = (
        f"scale={w * 4}:{h * 4},"
        f"zoompan=z='min(zoom+0.0006,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
        "format=yuv420p"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", str(still), "-t", f"{d:.3f}", "-vf", vf,
         "-r", str(fps), "-an", str(out)],
        check=True, capture_output=True,
    )
    return out


def handler(event: bytes, context) -> str:
    """Function Compute handler. Event: one render job for one shot."""
    job = json.loads(event)
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        if caption := job.get("caption"):
            job["caption"] = _tighten_caption(caption, job.get("caption_max_words", 12))
        clip = _render(job, workdir)
        out_key = job["output_key"]
        _bucket().put_object_from_file(out_key, str(clip))
    return json.dumps({"status": "ok", "output_key": out_key})
