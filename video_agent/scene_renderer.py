# DEPRECATED: Not used in browser-first architecture. Kept for reference.
"""Per-scene and full-video rendering system.

Provides render_all_scenes() which decides between monolithic (full-video)
and per-scene parallel rendering based on scene count and framework mix.
"""

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google import genai
from google.genai.types import GenerateContentConfig, ThinkingConfig

from .remotion_tools import render_remotion
from .tools import BASE_OUTPUT_DIR, _get_media_duration, _update_status, render_manim

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
CODE_GEN_MODEL = "gemini-3-flash-preview"

REMOTION_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "remotion_project"
FPS = 30
MAX_CODE_RETRIES = 3

def _get_max_render_workers() -> int:
    """Number of parallel render workers. Higher for Cloud Run (offloaded),
    lower for local (CPU-bound). Configurable via MAX_RENDER_WORKERS env var."""
    explicit = os.environ.get("MAX_RENDER_WORKERS")
    if explicit:
        return int(explicit)
    backend_m = os.environ.get("MANIM_RENDER_BACKEND", "local").lower()
    backend_r = os.environ.get("REMOTION_RENDER_BACKEND", "local").lower()
    if backend_m == "cloudrun" or backend_r == "cloudrun":
        return 4
    return 2

_THINKING = GenerateContentConfig(
    thinking_config=ThinkingConfig(thinking_budget=8000),
)

# ---------------------------------------------------------------------------
# Prompt fragments for per-scene code generation
# ---------------------------------------------------------------------------

# These import the full API references from the agent modules but change
# the workflow section to target a SINGLE scene instead of a full video.

REMOTION_SCENE_PROMPT = r"""You are an expert Remotion (React/TypeScript) code generator. You generate code for a SINGLE SCENE of a video.

TASK: Generate a COMPLETE, self-contained Remotion component for ONE scene.

RULES:
  - Named export: GeneratedComp
  - fps=30, resolution 1920x1080
  - Convert seconds to frames: Math.ceil(seconds * 30)
  - The component MUST cover exactly the given duration in frames
  - Add 15 extra frames safety buffer
  - MUST include a 15-frame fade-in at the very start (interpolate frame [0,15] opacity 0->1)
  - MUST include a 15-frame fade-out at the very end (interpolate frame [dur-15,dur] opacity 1->0)
  - ALL animation via useCurrentFrame() + interpolate/spring, NEVER CSS transitions
  - ALWAYS include extrapolateLeft/Right: 'clamp' in interpolate()
  - NEVER use Math.random() — use random('seed') from 'remotion'
  - Use pre-generated images via staticFile('filename.jpg')
  - Follow the STYLE CONSTANTS exactly for colors, fonts, sizes

PERFORMANCE (Cloud Run has NO GPU — these CSS properties are SLOW, avoid them):
  - NEVER use: box-shadow, text-shadow, filter: blur(), filter: drop-shadow()
  - NEVER use: CSS gradients (linear-gradient, radial-gradient) on large areas
  - INSTEAD use: solid colors, opacity, transform (translate/scale/rotate), border
  - For gradient backgrounds: use a pre-generated gradient IMAGE via staticFile()
  - For glow effects: use border with rgba color instead of box-shadow
  - These substitutions make rendering 2-4x faster on Cloud Run

IMPORTS AVAILABLE:
import {useCurrentFrame, useVideoConfig, AbsoluteFill, Img,
        interpolate, spring, Sequence, Series, staticFile, random} from 'remotion';
import {fade} from '@remotion/transitions/fade';
import {loadFont} from '@remotion/google-fonts/Roboto';
import {evolvePath} from '@remotion/paths';
import {fitText} from '@remotion/layout-utils';

Output ONLY the TypeScript/React code. No explanations, no markdown fences."""


MANIM_SCENE_PROMPT = r"""You are an expert Manim code generator. You generate code for a SINGLE SCENE of a video.

TASK: Generate a COMPLETE, self-contained Manim Python script for ONE scene.

RULES:
  - First line: from manim import *
  - May also import numpy as np and math
  - Class MUST be named GeneratedScene(Scene)
  - Animation time MUST be >= the given audio duration in seconds
  - Better slightly OVER than under
  - Add self.wait(0.5) safety buffer at the end
  - Use run_time= on self.play() to control durations
  - NEVER import external packages beyond manim, numpy, math
  - NEVER use 3D scenes (ThreeDScene)
  - Keep all content within screen bounds (-7 to 7 horizontal, -4 to 4 vertical)
  - Use ImageMobject(r"<absolute_path>") for pre-generated images
  - Follow the STYLE CONSTANTS for colors and visual design

Output ONLY the Python code. No explanations, no markdown fences."""


# ---------------------------------------------------------------------------
# Code extraction helper
# ---------------------------------------------------------------------------

def _extract_code(text: str) -> str:
    """Extract code from LLM response, stripping markdown fences if present."""
    fence = re.search(r"```(?:tsx?|typescript|python|py)?\s*\n(.*?)```", text, re.S)
    if fence:
        return fence.group(1).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Per-scene narration overlay
# ---------------------------------------------------------------------------

def _overlay_narration(video_path: str, audio_path: str, output_path: str) -> str:
    """Merge a scene's video with its narration audio via FFmpeg.

    Handles duration mismatches: pads video if audio is longer,
    trims video if it's longer. Returns path to the merged file.
    """
    if not os.path.exists(audio_path):
        shutil.copy2(video_path, output_path)
        print(f"    [OVERLAY] No audio found, copying video as-is")
        return output_path

    video_dur = _get_media_duration(video_path)
    audio_dur = _get_media_duration(audio_path)

    if audio_dur > video_dur + 0.5:
        pad = audio_dur - video_dur + 1.0
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path, "-i", audio_path,
            "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={pad:.2f}[v]",
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            output_path,
        ]
    elif video_dur > audio_dur + 0.5:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path, "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path, "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            output_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"    [OVERLAY] FFmpeg failed: {result.stderr[-500:]}")
        shutil.copy2(video_path, output_path)
        return output_path

    return output_path


# ---------------------------------------------------------------------------
# Single scene rendering (per-scene mode)
# ---------------------------------------------------------------------------

def _render_single_scene(
    video_id: str,
    scene: dict,
    style_constants: dict,
    audio_dir: Path,
    images_dir: Path,
    workspace_dir: Path | None = None,
) -> dict:
    """Render one scene: generate code via Gemini, render, overlay narration."""
    scene_id = scene["id"]
    framework = scene.get("framework", "remotion")
    duration = scene.get("audio_duration", 10.0)
    scene_dir = BASE_OUTPUT_DIR / video_id / "scenes" / scene_id
    scene_dir.mkdir(parents=True, exist_ok=True)

    _update_status(video_id, "processing", f"rendering_scene_{scene_id}")

    print(f"  [SCENE] Rendering '{scene_id}' ({framework}, {duration:.1f}s)...")

    # Build the scene-specific prompt
    style_block = "STYLE CONSTANTS (MUST follow exactly):\n"
    for k, v in style_constants.items():
        style_block += f"  {k}: {v}\n"

    image_block = ""
    listed_files = set()

    if scene.get("asset_results"):
        image_block = "\nPRE-GENERATED IMAGES FOR THIS SCENE:\n"
        for ar in scene["asset_results"]:
            if ar.get("type") == "image" and ar.get("filename"):
                listed_files.add(ar["filename"])
                if framework == "remotion":
                    image_block += f"  - staticFile('{ar['filename']}') — {ar.get('usage', '')}\n"
                else:
                    path = ar.get("path", "")
                    image_block += f"  - ImageMobject(r\"{path}\") — {ar.get('usage', '')}\n"

    # Also list ALL available images so the LLM sees what actually exists
    if images_dir.exists():
        extra_images = []
        for img_file in images_dir.iterdir():
            if img_file.is_file() and img_file.name not in listed_files:
                extra_images.append(img_file.name)
        if extra_images:
            if not image_block:
                image_block = "\n"
            image_block += "\nOTHER AVAILABLE IMAGES (can also use these):\n"
            for fname in sorted(extra_images):
                if framework == "remotion":
                    image_block += f"  - staticFile('{fname}')\n"
                else:
                    image_block += f"  - ImageMobject(r\"{images_dir / fname}\")\n"

    if image_block:
        image_block += (
            "\nCRITICAL: ONLY use filenames listed above. "
            "Do NOT invent or guess image filenames — if a filename is not listed, do not use it.\n"
        )

    scene_description = (
        f"SCENE: {scene.get('title', scene_id)}\n"
        f"Duration: {duration} seconds ({int(duration * 30)} frames at 30fps)\n"
        f"Layout type: {scene.get('layout_type', 'CinematicTitle')}\n"
        f"Visual description: {scene.get('visual_description', '')}\n"
        f"Animation notes: {scene.get('animation_notes', '')}\n"
    )

    if framework == "remotion":
        base_prompt = REMOTION_SCENE_PROMPT
    else:
        base_prompt = MANIM_SCENE_PROMPT

    full_prompt = f"{base_prompt}\n\n{style_block}\n{scene_description}\n{image_block}"

    # Generate code via Gemini directly
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    generated_code = None
    render_result = None

    for attempt in range(1, MAX_CODE_RETRIES + 1):
        try:
            if attempt == 1:
                contents = full_prompt
            else:
                contents = (
                    f"{full_prompt}\n\n"
                    f"PREVIOUS ATTEMPT FAILED with this error:\n"
                    f"{render_result.get('stderr', '')[:2000]}\n"
                    f"{render_result.get('error_message', '')}\n\n"
                    f"Fix the code and try again. Output ONLY the corrected code."
                )

            response = client.models.generate_content(
                model=CODE_GEN_MODEL,
                contents=contents,
                config=_THINKING,
            )

            raw_text = ""
            for part in response.candidates[0].content.parts:
                if part.text:
                    raw_text += part.text

            generated_code = _extract_code(raw_text)

        except Exception as e:
            print(f"    [SCENE] Code generation failed (attempt {attempt}): {e}")
            if attempt == MAX_CODE_RETRIES:
                return {"scene_id": scene_id, "status": "error", "error": str(e)}
            time.sleep(2)
            continue

        # Save the generated code
        if framework == "remotion":
            code_path = scene_dir / "code.tsx"
        else:
            code_path = scene_dir / "code.py"
        code_path.write_text(generated_code, encoding="utf-8")

        # Render
        if framework == "remotion":
            render_result = render_remotion(
                video_id=video_id,
                remotion_code=generated_code,
                duration_in_seconds=duration + 1.0,
            )
        else:
            render_result = render_manim(
                video_id=video_id,
                manim_code=generated_code,
            )

        if render_result.get("status") == "success":
            break

        print(f"    [SCENE] Render failed (attempt {attempt}/{MAX_CODE_RETRIES})")
        if attempt == MAX_CODE_RETRIES:
            return {
                "scene_id": scene_id,
                "status": "error",
                "error": render_result.get("error_message", "Render failed after retries"),
            }

    rendered_path = render_result["video_path"]
    rendered_copy = str(scene_dir / "rendered.mp4")
    shutil.copy2(rendered_path, rendered_copy)

    # Overlay narration audio
    audio_path = str(audio_dir / f"{scene_id}.wav")
    final_path = str(scene_dir / "final.mp4")
    _overlay_narration(rendered_copy, audio_path, final_path)

    final_dur = _get_media_duration(final_path)
    print(f"  [SCENE] '{scene_id}' done ({final_dur:.1f}s): {final_path}")

    return {
        "scene_id": scene_id,
        "status": "success",
        "video_path": final_path,
        "duration": round(final_dur, 2),
    }


# ---------------------------------------------------------------------------
# Parallel per-scene rendering
# ---------------------------------------------------------------------------

def _render_scenes_parallel(video_id: str, plan: dict, scenes: list) -> list:
    """Render all scenes in parallel using ThreadPoolExecutor."""
    audio_dir = BASE_OUTPUT_DIR / video_id / "audio"
    images_dir = BASE_OUTPUT_DIR / video_id / "images"
    style_constants = plan.get("style_constants", {})
    total = len(scenes)

    workers = _get_max_render_workers()
    print(f"  [RENDER] Parallel mode: {total} scenes, {workers} workers")

    results = [None] * total

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {}
        for i, scene in enumerate(scenes):
            future = pool.submit(
                _render_single_scene,
                video_id, scene, style_constants,
                audio_dir, images_dir,
            )
            future_to_idx[future] = i

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            scene_id = scenes[idx]["id"]
            try:
                result = future.result()
                results[idx] = result
                done = sum(1 for r in results if r is not None)
                _update_status(
                    video_id, "processing",
                    f"rendered_scene_{done}_of_{total}",
                )
                print(f"  [RENDER] {done}/{total} scenes complete")
            except Exception as e:
                results[idx] = {
                    "scene_id": scene_id,
                    "status": "error",
                    "error": str(e),
                }

    return results


# ---------------------------------------------------------------------------
# Full-video fallback rendering (delegates to ADK agents)
# ---------------------------------------------------------------------------

def _render_full_video(video_id: str, plan: dict, scenes: list) -> dict:
    """Render all scenes as a single video using the existing ADK agent.

    Runs synchronously via a direct Gemini API call (not ADK runner) for
    simplicity. Constructs the same prompt format the ADK agents expect.
    """
    framework = scenes[0].get("framework", "remotion")
    style_constants = plan.get("style_constants", {})
    audio_dir = BASE_OUTPUT_DIR / video_id / "audio"
    images_dir = BASE_OUTPUT_DIR / video_id / "images"

    _update_status(video_id, "processing", f"rendering_full_video_{framework}")
    print(f"  [RENDER] Full-video mode ({framework}, {len(scenes)} scenes)")

    # Build the delegation message matching the existing agent prompts
    style_block = "STYLE CONSTANTS (MUST follow exactly):\n"
    for k, v in style_constants.items():
        style_block += f"  {k}: {v}\n"

    sections_text = ""
    total_duration = 0.0
    for i, sc in enumerate(scenes):
        dur = sc.get("audio_duration", 8.0)
        total_duration += dur
        sections_text += (
            f"  Section {i+1}: '{sc['id']}' ({dur:.1f}s) — {sc.get('title', '')} "
            f"— Visual: {sc.get('layout_type', 'CinematicTitle')}\n"
            f"    Description: {sc.get('visual_description', '')}\n"
            f"    Animation: {sc.get('animation_notes', '')}\n"
        )

    image_block = "Pre-generated images available:\n"
    if images_dir.exists():
        for img_file in images_dir.iterdir():
            if img_file.is_file():
                if framework == "remotion":
                    image_block += f"  - {img_file.name}: staticFile('{img_file.name}')\n"
                else:
                    image_block += f"  - {img_file.name}: ImageMobject(r\"{img_file.resolve()}\")\n"

    prompt = (
        f"Generate and render a complete video with these sections:\n"
        f"{sections_text}\n"
        f"Video ID: {video_id}\n"
        f"Total duration: {total_duration:.1f} seconds\n\n"
        f"{style_block}\n"
        f"IMPORTANT: Use DIFFERENT visual template for each section!\n\n"
        f"{image_block}"
    )

    if framework == "remotion":
        from .remotion_agent import REMOTION_INSTRUCTION
        system_prompt = REMOTION_INSTRUCTION
    else:
        from .agent import MANIM_INSTRUCTION
        system_prompt = MANIM_INSTRUCTION

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    for attempt in range(1, MAX_CODE_RETRIES + 1):
        try:
            if attempt > 1:
                prompt += f"\n\nPREVIOUS RENDER FAILED:\n{render_result.get('stderr', '')[:2000]}\nFix and try again."

            response = client.models.generate_content(
                model=CODE_GEN_MODEL,
                contents=f"{system_prompt}\n\n{prompt}",
                config=_THINKING,
            )

            raw_text = ""
            for part in response.candidates[0].content.parts:
                if part.text:
                    raw_text += part.text

            generated_code = _extract_code(raw_text)

        except Exception as e:
            print(f"  [RENDER] Full-video code gen failed (attempt {attempt}): {e}")
            if attempt == MAX_CODE_RETRIES:
                return {"status": "error", "error": str(e)}
            time.sleep(2)
            continue

        # Save code
        code_dir = BASE_OUTPUT_DIR / video_id / "scenes" / "_full_video"
        code_dir.mkdir(parents=True, exist_ok=True)
        ext = "tsx" if framework == "remotion" else "py"
        (code_dir / f"code.{ext}").write_text(generated_code, encoding="utf-8")

        if framework == "remotion":
            render_result = render_remotion(
                video_id=video_id,
                remotion_code=generated_code,
                duration_in_seconds=total_duration + 2.0,
            )
        else:
            render_result = render_manim(
                video_id=video_id,
                manim_code=generated_code,
            )

        if render_result.get("status") == "success":
            print(f"  [RENDER] Full-video render succeeded: {render_result['video_path']}")
            return {
                "status": "success",
                "video_path": render_result["video_path"],
                "render_mode": "full_video",
            }

        print(f"  [RENDER] Full-video render failed (attempt {attempt})")

    return {"status": "error", "error": "Full-video render failed after retries"}


# ---------------------------------------------------------------------------
# Main entry point — render_all_scenes tool
# ---------------------------------------------------------------------------

def render_all_scenes(video_id: str, plan_json: str) -> dict:
    """Render all scenes from the enhanced plan JSON.

    Decides between full-video (monolithic) and per-scene (parallel) rendering:
    - Full-video: all scenes use same framework AND < 6 scenes
    - Per-scene: hybrid framework or >= 6 scenes

    Args:
        video_id: Unique identifier for this video project.
        plan_json: The enhanced plan JSON string containing scenes with
            audio_duration, visual_description, animation_notes, etc.

    Returns:
        dict with:
          - status: "success", "partial", or "error"
          - render_mode: "full_video" or "per_scene"
          - scene_videos: list of {scene_id, video_path, duration} for per-scene
          - video_path: single path for full-video mode
    """
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid plan JSON: {e}"}

    scenes = plan.get("scenes", [])
    if not scenes:
        return {"status": "error", "error_message": "No scenes in plan."}

    _update_status(video_id, "processing", "rendering_scenes")

    # Decide rendering mode
    frameworks = set(sc.get("framework", "remotion") for sc in scenes)
    use_full_video = len(frameworks) == 1 and len(scenes) < 6

    if use_full_video:
        print(f"  [RENDER] Using full-video fallback ({len(scenes)} scenes, single framework)")
        result = _render_full_video(video_id, plan, scenes)
        return result

    # Per-scene parallel rendering
    results = _render_scenes_parallel(video_id, plan, scenes)

    succeeded = [r for r in results if r and r.get("status") == "success"]
    failed = [r for r in results if r and r.get("status") != "success"]

    if not succeeded:
        return {
            "status": "error",
            "error_message": f"All {len(scenes)} scenes failed to render.",
            "failed_scenes": [r.get("scene_id") for r in failed],
        }

    scene_videos = []
    for r in results:
        if r and r.get("status") == "success":
            scene_videos.append({
                "scene_id": r["scene_id"],
                "video_path": r["video_path"],
                "duration": r.get("duration", 0),
            })

    status = "success" if len(succeeded) == len(scenes) else "partial"
    skipped = [r.get("scene_id") for r in failed] if failed else []

    if skipped:
        print(f"  [RENDER] WARNING: Skipped {len(skipped)} failed scenes: {skipped}")

    print(f"  [RENDER] Done: {len(succeeded)}/{len(scenes)} scenes rendered")

    return {
        "status": status,
        "render_mode": "per_scene",
        "scene_videos": scene_videos,
        "scenes_rendered": len(succeeded),
        "scenes_total": len(scenes),
        "skipped_scenes": skipped,
        "message": f"Rendered {len(succeeded)}/{len(scenes)} scenes.",
    }
