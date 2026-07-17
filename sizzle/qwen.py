"""Thin client over DashScope for the text and vision models on the critical path.

Every call returns (content, tokens_spent) so the manifest can account for spend.
In dry-run mode no network call is made; callers supply their own stubs upstream.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus

from .config import Config


class QwenError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """Models wrap JSON in prose or fences despite instructions; dig it out."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise QwenError(f"model did not return JSON: {text[:200]!r}")


def chat(cfg: Config, model: str, system: str, user: str) -> tuple[str, int]:
    from dashscope import Generation

    cfg.apply_endpoints()
    rsp = Generation.call(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        result_format="message",
    )
    if rsp.status_code != HTTPStatus.OK:
        raise QwenError(f"{model}: {rsp.code} {rsp.message}")
    content = rsp.output.choices[0].message.content
    usage = rsp.usage or {}
    tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    return content, tokens


def chat_json(cfg: Config, model: str, system: str, user: str) -> tuple[dict, int]:
    content, tokens = chat(cfg, model, system, user)
    return _extract_json(content), tokens


def vision_json(cfg: Config, model: str, system: str, user_text: str, image_paths: list[str]) -> tuple[dict, int]:
    """Ask a vision model a question about local image files; expect JSON back."""
    from dashscope import MultiModalConversation

    cfg.apply_endpoints()
    content: list[dict] = [{"image": f"file://{p}"} for p in image_paths]
    content.append({"text": user_text})
    rsp = MultiModalConversation.call(
        model=model,
        messages=[
            {"role": "system", "content": [{"text": system}]},
            {"role": "user", "content": content},
        ],
    )
    if rsp.status_code != HTTPStatus.OK:
        raise QwenError(f"{model}: {rsp.code} {rsp.message}")
    parts = rsp.output.choices[0].message.content
    text = "".join(p.get("text", "") for p in parts) if isinstance(parts, list) else str(parts)
    usage = rsp.usage or {}
    tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    return _extract_json(text), tokens
