"""Run configuration. Everything tunable lives here; nothing downstream reads env vars directly."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

INTL_BASE_HTTP = "https://dashscope-intl.aliyuncs.com/api/v1"
INTL_BASE_WS = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"


@dataclass
class ModelRouting:
    story: str = "qwen3.7-max"
    capture: str = "qwen3.7-plus"
    critic: str = "qwen3.7-plus"
    t2v: str = "happyhorse-1.1-t2v"
    i2v: str = "happyhorse-1.1-i2v"
    r2v: str = "happyhorse-1.1-r2v"
    t2v_fallback: str = "wan2.7-t2v"
    tts: str = "cosyvoice-v3-plus"
    tts_voice: str = "longanyang"


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: os.environ.get("DASHSCOPE_API_KEY", ""))
    work_dir: Path = field(default_factory=lambda: Path("runs"))
    models: ModelRouting = field(default_factory=ModelRouting)

    target_duration_s: int = 180
    duration_tolerance_s: int = 5
    money_shot_deadline_s: int = 45
    max_dead_air_s: float = 2.0

    # Budget allocator: GENERATE spend cap, in abstract token units.
    # Video-second costs below convert clip seconds into these units.
    generate_budget_tokens: int = 600_000
    tokens_per_generate_second: int = 30_000

    # Critic loop
    max_critic_iterations: int = 3
    score_epsilon: float = 0.02

    # Capture agent
    capture_max_attempts: int = 3
    capture_viewport: tuple[int, int] = (1280, 720)

    # Assembly
    resolution: tuple[int, int] = (1280, 720)
    fps: int = 24

    # Dry-run: replace every network/model call with a deterministic local stub.
    dry_run: bool = False

    def apply_endpoints(self) -> None:
        import dashscope

        dashscope.base_http_api_url = INTL_BASE_HTTP
        dashscope.base_websocket_api_url = INTL_BASE_WS
        if self.api_key:
            dashscope.api_key = self.api_key
