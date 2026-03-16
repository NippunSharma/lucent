"""Scene planner — generates a structured scene plan from context.

Uses Gemini with thinking and Google Search to create a detailed plan
specifying scenes, narrations, ManimGL approaches, and reference mappings.
"""

import json
import logging
import os
import re
import time

from google import genai
from google.genai.types import (
    GenerateContentConfig,
    GoogleSearch,
    ThinkingConfig,
    Tool,
)

from .context_processor import Context
from .prompt_templates import PLANNER_PROMPT

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
PLANNER_MODEL = "gemini-3-flash-preview"
THINKING_BUDGET = 16000


def _extract_json(text: str) -> dict:
    """Extract JSON from a response that may contain markdown fences."""
    clean = text.strip()
    if clean.startswith("```"):
        fence = re.search(r"```(?:json)?\s*\n(.*?)```", clean, re.S)
        if fence:
            clean = fence.group(1).strip()
    return json.loads(clean)


def _validate_plan(plan: dict) -> dict:
    """Validate and normalize the scene plan."""
    if "scenes" not in plan:
        raise ValueError("Plan missing 'scenes' key")

    scenes = plan["scenes"]
    if not isinstance(scenes, list) or len(scenes) < 1:
        raise ValueError("Plan must have at least 1 scene")

    if len(scenes) > 16:
        logger.warning("Plan has %d scenes, truncating to 16", len(scenes))
        scenes = scenes[:16]
        plan["scenes"] = scenes

    for i, scene in enumerate(scenes):
        if "id" not in scene:
            scene["id"] = f"scene_{i + 1}"
        if "title" not in scene:
            scene["title"] = f"Scene {i + 1}"
        if "narration" not in scene:
            scene["narration"] = ""
        if "visual_description" not in scene:
            scene["visual_description"] = scene.get("title", "")
        if "manim_approach" not in scene:
            scene["manim_approach"] = "Use basic Text and Tex animations"
        if "estimated_duration" not in scene:
            scene["estimated_duration"] = 30
        if "references" not in scene:
            scene["references"] = []
        if "act" not in scene:
            if i == 0:
                scene["act"] = 1
            elif i >= len(scenes) - 1:
                scene["act"] = 3
            else:
                scene["act"] = 2

    plan["total_scenes"] = len(scenes)
    return plan


def plan_scenes(context: Context, format_instructions: str = "") -> dict:
    """Generate a scene plan from the processed context.

    Args:
        context: Processed context with topic, research brief, and references.
        format_instructions: Video format guidance (scene count, duration, narration length).

    Returns:
        A validated scene plan dict.
    """
    logger.info("Planning scenes for topic: '%s'", context.topic)
    t0 = time.time()

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    if not format_instructions:
        format_instructions = (
            "Plan 4-12 scenes for a long-form educational video (2-5 minutes total). "
            "Each scene should have 4-6 sentences of narration (~20-40 seconds per scene)."
        )

    planner_prompt = PLANNER_PROMPT.replace("{format_instructions}", format_instructions)
    prompt = f"{planner_prompt}\n\n## Input Context\n\n{context.to_prompt_string()}"

    try:
        response = client.models.generate_content(
            model=PLANNER_MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=THINKING_BUDGET),
                tools=[Tool(google_search=GoogleSearch())],
            ),
        )
    except Exception as e:
        logger.warning("Planner grounded search failed (%s), retrying without search", e)
        response = client.models.generate_content(
            model=PLANNER_MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=THINKING_BUDGET),
            ),
        )

    raw_text = ""
    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.text:
                raw_text += part.text

    if not raw_text.strip():
        raise RuntimeError("Planner returned empty response")

    try:
        plan = _extract_json(raw_text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse planner JSON: %s\nRaw: %s", e, raw_text[:500])
        raise RuntimeError(f"Planner returned invalid JSON: {e}") from e

    plan = _validate_plan(plan)

    elapsed = time.time() - t0
    logger.info(
        "Plan generated in %.1fs: %d scenes, est. %ds total",
        elapsed,
        plan["total_scenes"],
        sum(s.get("estimated_duration", 30) for s in plan["scenes"]),
    )
    return plan
