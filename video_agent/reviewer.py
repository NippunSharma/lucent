"""Plan reviewer tool — code-driven visuals, depth checks, slideshow detection.

This is a TOOL FUNCTION (not an ADK agent) that calls Gemini directly.
It reviews the orchestrator's plan and TTS results, checks for
content depth, flags slideshow patterns, verifies narrative arc, and
enriches animation choreography with frame-level timing.

All visuals are code-driven (CSS, SVG, React components). No AI image generation.
"""

import json
import os
import re
import time
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from google.genai.types import GenerateContentConfig, ThinkingConfig

from .tools import BASE_OUTPUT_DIR, _update_status

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
REVIEW_MODEL = "gemini-3-flash-preview"

REVIEWER_PROMPT = r"""You are a senior motion-graphics director and creative reviewer for CODE-DRIVEN animated videos built with Remotion. All visuals are built from React components, SVG, CSS gradients, and animations — NO AI-generated images. Your job is to review and enhance the plan.

CORE PRINCIPLE: ALL VISUALS ARE CODE-DRIVEN
  Every background, diagram, chart, illustration, and decoration must be built from:
  - CSS gradients (linear, radial, conic) + noise/grid overlays
  - Inline SVG paths for icons, diagrams, illustrations, flowcharts
  - React components: DataChart, ProcessFlow, FlowChart, SVGPathReveal, etc.
  - CSS animations, spring physics, keyframe-based motion
  - Canvas / Three.js for 3D elements
  NEVER suggest or add image assets. The only images allowed are user-provided reference
  images (product photos, logos) already in the plan.

AVAILABLE REMOTION COMPONENTS (the code generator can use these):
  CinematicTitle, SplitScreen, ProcessFlow, ComparisonColumns, TimelineView,
  QuoteCard, DataChart, AnimatedText, ParticleField,
  CountUpNumber, SVGPathReveal, TypewriterText, MathEquation, CameraContainer,
  ProgressBar, ChapterIndicator, InteractiveQuiz, FlowChart, CharacterNarrator

REVIEW CHECKLIST:

1. NARRATIVE ARC — verify the plan follows a 3-act structure:
   - Act 1 (Hook & Context, ~20%): opens with surprising fact/question, establishes relevance
   - Act 2 (Deep Dive, ~60%): builds concepts progressively, one idea per scene
   - Act 3 (Synthesis & Takeaway, ~20%): connects everything, real-world applications, memorable close
   Flag if the plan is a flat topic list instead of a story.

2. CONTENT DEPTH — check each scene:
   - Narration should be 4-6 sentences (not 1-2 skimpy sentences)
   - Total narration should be 900-1200 words (~6-8 minutes)
   - If narration is too thin, REWRITE it with richer detail (keep the scene structure)

3. SLIDESHOW DETECTION — flag these anti-patterns:
   - Static text on plain background without motion (CRITICAL — must have animation)
   - Same layout_type used >3 times
   - No CameraContainer usage (should be on 30%+ of scenes)
   - Missing animation layers (each scene needs bg + content + accent)
   - No CountUpNumber or DataChart scenes (needs data visualization)
   - Vague visual_description like "beautiful visual" instead of specific CSS/SVG/component specs

4. VISUAL VARIETY — ensure:
   - At least 6 different layout_type values used across scenes
   - Mix of ProcessFlow, DataChart, ComparisonColumns, TimelineView
   - At least 2 scenes use SVGPathReveal or MathEquation
   - At least 1 InteractiveQuiz scene (ONLY if the plan already includes one — do NOT add quizzes if the plan has none)

5. VISUAL DESCRIPTION QUALITY — each scene's visual_description should specify:
   - EXACT background: CSS gradient spec (e.g. "radial-gradient(circle at 50% 30%, #1a1a2e 0%, #0d1117 100%)")
   - Overlay effects: grid lines, noise, vignette, particle specs
   - Content composition: what components, where positioned, what data they show
   - SVG shapes: describe the paths/diagrams to draw (NOT "use an image of a diagram")
   If visual_description is vague, REWRITE it with specific code-driven specs.

6. ANIMATION CHOREOGRAPHY — enrich animation_notes with:
   - Frame-level timing: "AnimatedText starts frame 10, stagger 4"
   - Spring configs: "damping: 14, stiffness: 100"
   - CameraContainer params: "zoom 1.0→1.05, panX 0→-20"
   - ParticleField specs: "count 30, color accent, opacity 0.2"
   - Fade in/out: "15-frame fade in, 15-frame fade out"
   - SVG draw animation: "stroke-dashoffset from 1 to 0 over 60 frames"
   - Data bar growth: "stagger 8 frames, spring(damping:14, stiffness:80)"

OUTPUT FORMAT (strict JSON):
{
  "analysis": "Detailed paragraph summarizing all changes, issues found, and improvements made",
  "slideshow_warnings": ["warning 1", "warning 2"],
  "depth_score": 1-10,
  "variety_score": 1-10,
  "narrative_arc_score": 1-10,
  "enhanced_plan": { ...the complete enhanced plan JSON... }
}

RULES for enhanced_plan:
- SAME structure as input: same scene IDs and order
- You MAY enrich: visual_description, animation_notes, layout_type, narration (if too thin)
- You MUST NOT change: id, order, audio_duration
- You MUST NOT add image assets or suggest image generation — all visuals are code-driven
- You MAY add a "quiz" field ONLY if the input plan already contains InteractiveQuiz scenes or quiz fields. If the plan has NO quizzes, do NOT add any.
- You MUST add "act": 1|2|3 to each scene based on narrative position
- If any scene has an "assets" array with AI-generated images (has "prompt" or "tool" fields), REMOVE those entries. Only keep user-provided reference images (entries with just "filename" and "usage", no "prompt" or "tool").

Output ONLY valid JSON. No markdown fences, no extra text."""


def review_plan(video_id: str, plan_json: str, asset_results_json: str) -> dict:
    """Review and enhance the orchestrator's video plan.

    Checks content depth, slideshow detection, narrative arc, and
    enriches animation choreography. No image review — all visuals are code-driven.

    Args:
        video_id: Unique identifier for this video project.
        plan_json: The orchestrator's plan JSON string.
        asset_results_json: JSON string with TTS/asset generation results.

    Returns:
        dict with status, enhanced_plan, analysis, and quality scores.
    """
    _update_status(video_id, "processing", "reviewing_plan")

    try:
        plan = json.loads(plan_json)
        asset_results = json.loads(asset_results_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid JSON: {e}"}

    project_dir = BASE_OUTPUT_DIR / video_id
    project_dir.mkdir(parents=True, exist_ok=True)

    with open(project_dir / "plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)

    contents = [
        f"{REVIEWER_PROMPT}\n\n"
        f"=== PLAN TO REVIEW ===\n{json.dumps(plan, indent=2)}\n\n"
        f"=== TTS GENERATION RESULTS ===\n{json.dumps(asset_results, indent=2)}"
    ]

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    print(f"  [REVIEWER] Reviewing plan (code-driven visuals, no image review)...")
    t0 = time.time()

    try:
        response = client.models.generate_content(
            model=REVIEW_MODEL,
            contents=contents,
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=12000),
            ),
        )
    except Exception as e:
        print(f"  [REVIEWER] Gemini call failed: {e}")
        return {
            "status": "error",
            "error_message": f"Review failed: {e}",
            "enhanced_plan": plan,
        }

    elapsed = time.time() - t0

    if not response.candidates or not response.candidates[0].content or not response.candidates[0].content.parts:
        print("  [REVIEWER] Gemini returned no candidates (safety filter or quota)")
        return {
            "status": "partial",
            "enhanced_plan": plan,
            "analysis_summary": "Review failed: model returned empty response.",
        }

    raw_text = ""
    for part in response.candidates[0].content.parts:
        if part.text:
            raw_text += part.text

    clean = raw_text.strip()
    if clean.startswith("```"):
        fence = re.search(r"```(?:json)?\s*\n(.*?)```", clean, re.S)
        if fence:
            clean = fence.group(1).strip()

    try:
        review_output = json.loads(clean)
    except json.JSONDecodeError:
        print(f"  [REVIEWER] Failed to parse JSON, using original plan")
        with open(project_dir / "review_analysis.txt", "w", encoding="utf-8") as f:
            f.write(f"REVIEW FAILED TO PARSE\n\nRaw response:\n{raw_text}")
        return {
            "status": "partial",
            "enhanced_plan": plan,
            "analysis_summary": "Review produced non-JSON output; using original plan.",
        }

    analysis = review_output.get("analysis", "No analysis provided.")
    enhanced_plan = review_output.get("enhanced_plan", plan)

    # Validate that the reviewer didn't break schema invariants
    orig_scenes = plan.get("scenes", [])
    enh_scenes = enhanced_plan.get("scenes", []) if isinstance(enhanced_plan, dict) else []
    if len(enh_scenes) != len(orig_scenes):
        print(f"  [REVIEWER] Warning: scene count changed ({len(orig_scenes)}->{len(enh_scenes)}), restoring original scenes")
        enhanced_plan["scenes"] = orig_scenes
    else:
        for orig, enh in zip(orig_scenes, enh_scenes):
            for key in ("id", "order", "audio_duration"):
                if key in orig:
                    enh[key] = orig[key]

    # Strip any AI-generated image assets the reviewer might have added
    stripped_images = 0
    for enh in (enhanced_plan.get("scenes", []) if isinstance(enhanced_plan, dict) else []):
        if "assets" in enh:
            cleaned = [a for a in enh["assets"] if not a.get("prompt") and not a.get("tool")]
            removed = len(enh["assets"]) - len(cleaned)
            if removed:
                stripped_images += removed
            enh["assets"] = cleaned if cleaned else []
            if not enh["assets"]:
                del enh["assets"]
    if stripped_images:
        print(f"  [REVIEWER] Stripped {stripped_images} AI-generated image asset(s) — all visuals are code-driven")

    orig_has_quiz = any(
        sc.get("quiz")
        or sc.get("layout_type") == "InteractiveQuiz"
        for sc in orig_scenes
    )
    if not orig_has_quiz:
        stripped = 0
        for enh in (enhanced_plan.get("scenes", []) if isinstance(enhanced_plan, dict) else []):
            if "quiz" in enh:
                del enh["quiz"]
                stripped += 1
            if enh.get("layout_type") == "InteractiveQuiz":
                enh["layout_type"] = "ProcessFlow"
                stripped += 1
            notes = enh.get("animation_notes", "")
            if "InteractiveQuiz" in notes:
                enh["animation_notes"] = re.sub(
                    r"InteractiveQuiz[^.]*\.", "", notes
                ).strip()
                stripped += 1
        if stripped:
            print(f"  [REVIEWER] Stripped {stripped} quiz reference(s) added by reviewer (quiz disabled)")

    slideshow_warnings = review_output.get("slideshow_warnings", [])
    depth_score = review_output.get("depth_score", 5)
    variety_score = review_output.get("variety_score", 5)
    arc_score = review_output.get("narrative_arc_score", 5)

    with open(project_dir / "enhanced_plan.json", "w", encoding="utf-8") as f:
        json.dump(enhanced_plan, f, indent=2)

    analysis_text = (
        f"Plan Review Analysis ({elapsed:.1f}s)\n"
        f"{'=' * 60}\n\n"
        f"Depth Score:    {depth_score}/10\n"
        f"Variety Score:  {variety_score}/10\n"
        f"Arc Score:      {arc_score}/10\n\n"
        f"Slideshow Warnings:\n"
    )
    for w in slideshow_warnings:
        analysis_text += f"  - {w}\n"
    analysis_text += f"\n{analysis}\n"

    with open(project_dir / "review_analysis.txt", "w", encoding="utf-8") as f:
        f.write(analysis_text)

    print(f"  [REVIEWER] Review complete in {elapsed:.1f}s (depth: {depth_score}, variety: {variety_score}, arc: {arc_score})")

    return {
        "status": "success",
        "enhanced_plan": enhanced_plan,
        "analysis_summary": analysis[:500],
        "quality_scores": {
            "depth": depth_score,
            "variety": variety_score,
            "narrative_arc": arc_score,
        },
        "slideshow_warnings": slideshow_warnings,
        "message": f"Plan reviewed in {elapsed:.1f}s (depth: {depth_score}, variety: {variety_score}, arc: {arc_score})",
    }
