"""Story engine (PRD §7.1): repo context in, valid ≤180s beat sheet out. qwen3.7-max."""

from __future__ import annotations

import json

from pydantic import ValidationError

from .config import Config
from .ingest import RepoContext
from .qwen import chat_json
from .schema import BeatSheet, ProjectInfo

SYSTEM = """You are a showrunner for software demo videos. Given a repository's README, \
manifests, and commit history, write a beat sheet for a demo video of at most {duration}s.

A demo video is a narrative with a fixed budget: establish a pain, raise stakes, reveal the \
mechanism, prove it works, land the ask. The money shot (the product visibly working) must \
land before 0:45.

Return ONLY a JSON object with this exact shape (no prose, no markdown fences):

{{
  "target_duration_s": {duration},
  "project": {{"name": "...", "one_liner": "...", "pain": "...", "mechanism": "..."}},
  "beats": [
    {{"id": "b01", "name": "cold_open", "intent": "...", "duration_s": 15,
      "narrative_weight": 0.9, "filmable": false, "is_static_information": false,
      "shots": ["s01"]}}
  ],
  "shots": [
    {{"id": "s01", "beat_id": "b01", "duration_s": 15,
      "spec": {{"kind": "generate", "prompt": "..."}} ,
      "acceptance": "closed visual predicate a VLM can check on a frame",
      "vo": "narration line, or null"}}
  ]
}}

Rules:
- Shot durations must sum to at most {duration}s. Aim for {duration}s exactly.
- 5 to 8 beats; each beat has 1 to 3 shots; shots are 5 to 20 seconds.
- spec.kind is one of: "generate" (prompt: cinematic text-to-video prompt),
  "capture" (goal: what the app should be seen doing — a goal, not a click script),
  "render" (template: one of "title_card", "code_snippet", "architecture", "metric_plate";
            payload: template data, e.g. {{"title": "...", "subtitle": "..."}} for title_card,
            {{"code": "...", "language": "...", "caption": "..."}} for code_snippet,
            {{"lines": ["..."]}} for architecture, {{"metrics": [{{"label": "...", "value": "..."}}]}} for metric_plate).
- Mark a beat filmable=true only if a live app could be filmed doing it.
- Mark is_static_information=true for beats that are just facts on screen (arch, metrics, titles).
- Every shot needs an acceptance predicate: one sentence, checkable on a single frame.
- VO lines: spoken-word pace is about 2.5 words per second; fit the shot duration.
- narrative_weight in [0,1]: how much of the story this beat carries."""


def _sanitize(raw: dict, duration: int) -> dict:
    """Normalize model output before validation: allocator owns `type`, so strip it if present."""
    raw.setdefault("target_duration_s", duration)
    for shot in raw.get("shots", []):
        shot.pop("type", None)
        spec = shot.get("spec", {})
        # tolerate models emitting shot-type names instead of spec kinds
        kind = str(spec.get("kind", "")).lower()
        spec["kind"] = {"generate": "generate", "capture": "capture", "render": "render"}.get(kind, kind)
    return raw


def _conform_duration(sheet: BeatSheet) -> BeatSheet:
    """Scale shot durations down proportionally if the model overshot the budget."""
    total = sheet.total_duration()
    if total <= sheet.target_duration_s:
        return sheet
    scale = sheet.target_duration_s / total
    for s in sheet.shots:
        s.duration_s = round(s.duration_s * scale, 1)
    for b in sheet.beats:
        b.duration_s = round(sum(s.duration_s for s in sheet.shots_for_beat(b.id)), 1)
    return sheet


def write_beat_sheet(cfg: Config, repo: RepoContext) -> tuple[BeatSheet, int]:
    if cfg.dry_run:
        return _stub_beat_sheet(cfg, repo), 0

    system = SYSTEM.format(duration=cfg.target_duration_s)
    user = repo.as_prompt_block()

    last_err: Exception | None = None
    tokens_total = 0
    for attempt in range(3):
        prompt = user if attempt == 0 else f"{user}\n\nYour previous output failed validation: {last_err}\nFix it and return only valid JSON."
        raw, tokens = chat_json(cfg, cfg.models.story, system, prompt)
        tokens_total += tokens
        try:
            sheet = BeatSheet.model_validate(_sanitize(raw, cfg.target_duration_s))
            return _conform_duration(sheet), tokens_total
        except (ValidationError, ValueError) as e:
            last_err = e
    raise RuntimeError(f"story engine failed validation 3 times: {last_err}")


def _stub_beat_sheet(cfg: Config, repo: RepoContext) -> BeatSheet:
    """Deterministic beat sheet for dry runs: exercises all three lanes."""
    d = cfg.target_duration_s
    data = {
        "target_duration_s": d,
        "project": ProjectInfo(
            name=repo.name,
            one_liner=f"{repo.name}, demonstrated in {d} seconds",
            pain="the demo video that never gets made",
            mechanism="a closed generate-verify loop",
        ).model_dump(),
        "beats": [
            {"id": "b01", "name": "cold_open", "intent": "establish the pain", "duration_s": 20,
             "narrative_weight": 0.9, "filmable": False, "is_static_information": False, "shots": ["s01"]},
            {"id": "b02", "name": "title", "intent": "name the product", "duration_s": 10,
             "narrative_weight": 0.5, "filmable": False, "is_static_information": True, "shots": ["s02"]},
            {"id": "b03", "name": "money_shot", "intent": "prove it works", "duration_s": 15,
             "narrative_weight": 1.0, "filmable": True, "is_static_information": False, "shots": ["s03"]},
            {"id": "b04", "name": "mechanism", "intent": "reveal how", "duration_s": 15,
             "narrative_weight": 0.7, "filmable": False, "is_static_information": True, "shots": ["s04"]},
            {"id": "b05", "name": "close", "intent": "land the ask", "duration_s": 10,
             "narrative_weight": 0.6, "filmable": False, "is_static_information": True, "shots": ["s05"]},
        ],
        "shots": [
            {"id": "s01", "beat_id": "b01", "duration_s": 20,
             "spec": {"kind": "generate", "prompt": "a builder at a desk at 3am, deadline looming, screen glow"},
             "acceptance": "frame shows a person at a desk at night, screen visible",
             "vo": "It's 3am. The deadline is in an hour. The video does not exist."},
            {"id": "s02", "beat_id": "b02", "duration_s": 10,
             "spec": {"kind": "render", "template": "title_card",
                      "payload": {"title": repo.name, "subtitle": "the demo video that makes itself"}},
             "acceptance": "frame shows the product name as a title card",
             "vo": f"This is {repo.name}."},
            {"id": "s03", "beat_id": "b03", "duration_s": 15,
             "spec": {"kind": "capture", "goal": "show the app's core flow producing visible output"},
             "acceptance": "frame shows the app UI with generated output visible",
             "vo": "Point it at a repo and a running app. It does the rest."},
            {"id": "s04", "beat_id": "b04", "duration_s": 15,
             "spec": {"kind": "render", "template": "architecture",
                      "payload": {"lines": ["repo -> story engine -> allocator", "three lanes: generate / capture / render", "critic watches the cut and recuts"]}},
             "acceptance": "frame shows an architecture diagram",
             "vo": "A story engine writes the beats. A critic watches the cut and sends it back."},
            {"id": "s05", "beat_id": "b05", "duration_s": 10,
             "spec": {"kind": "render", "template": "title_card",
                      "payload": {"title": repo.name, "subtitle": "your demo video, while you sleep"}},
             "acceptance": "frame shows a closing title card",
             "vo": "Ship the project. The video ships itself."},
        ],
    }
    return BeatSheet.model_validate(json.loads(json.dumps(data)))
