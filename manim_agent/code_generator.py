"""Manim Community Edition code generation with error recovery.

Generates scene code using Gemini, validates it, and provides error recovery.
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google import genai
from google.genai.types import (
    Content,
    GenerateContentConfig,
    GoogleSearch,
    Part,
    ThinkingConfig,
    Tool,
)

from .context_processor import Context
from .prompt_templates import (
    CODEGEN_SCENE_PROMPT,
    CODEGEN_SYSTEM_PROMPT,
    EDIT_PROMPT,
    ERROR_RECOVERY_PROMPT,
)

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
CODEGEN_MODEL = os.environ.get("CODEGEN_MODEL", "gemini-3-flash-preview")
CODEGEN_MODEL_FAST = os.environ.get("CODEGEN_MODEL_FAST", "gemini-3-flash-preview")
THINKING_BUDGET = int(os.environ.get("CODEGEN_THINKING_BUDGET", "32000"))
MAX_RETRIES = int(os.environ.get("CODEGEN_MAX_RETRIES", "3"))
MAX_PARALLEL_SCENES = int(os.environ.get("CODEGEN_MAX_PARALLEL", "2"))
API_RETRY_ATTEMPTS = 4
API_RETRY_BASE_DELAY = 5


def _api_call_with_retry(client, model, contents, config, scene_id="", call_label=""):
    """Make a Gemini API call with exponential backoff retry on 429 errors."""
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=config,
            )
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                if attempt < API_RETRY_ATTEMPTS - 1:
                    delay = API_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Codegen %s %s: rate limited (attempt %d/%d), retrying in %ds",
                        scene_id, call_label, attempt + 1, API_RETRY_ATTEMPTS, delay,
                    )
                    time.sleep(delay)
                    continue
            raise
    raise RuntimeError(f"API call failed after {API_RETRY_ATTEMPTS} attempts")


def _extract_code(text: str) -> str:
    """Extract Python code from response, handling markdown fences."""
    text = text.strip()

    # Find the largest fenced code block (greedy inner match)
    fences = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    if fences:
        # Pick the longest block (most likely the full scene code)
        text = max(fences, key=len).strip()

    # Remove any stray triple-backtick lines that survived extraction
    text = re.sub(r"^```\w*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```\s*$", "", text, flags=re.MULTILINE)

    # Remove non-ASCII control characters that models sometimes emit
    text = re.sub(r"[^\x09\x0a\x0d\x20-\x7e\x80-\uffff]", "", text)

    return text.strip()


def _validate_code(code: str) -> str:
    """Validate and fix common issues in generated ManimCE code."""
    # Ensure correct ManimCE import
    if "from manim import" not in code:
        code = "from manim import *\n\n" + code

    # Remove ManimGL import if present
    code = re.sub(r"from manimlib import.*\n?", "", code)

    # Remove interactive-only calls
    code = re.sub(r"self\.embed\(\).*\n?", "", code)

    # Fix ManimGL -> ManimCE API name mismatches
    code = re.sub(r"\bShowCreation\(", "Create(", code)
    code = re.sub(r"\bTexText\(", "Tex(", code)

    # ManimGL fix_in_frame -> ManimCE add_fixed_in_frame_mobjects
    code = re.sub(
        r"(\w+)\.fix_in_frame\(\)",
        r"self.add_fixed_in_frame_mobjects(\1)",
        code,
    )

    # ManimGL self.camera.frame.reorient / set_euler_angles -> ManimCE camera API
    # self.camera.frame.reorient(theta, phi) -> self.set_camera_orientation(phi=phi*DEGREES, theta=theta*DEGREES)
    # (Best effort — complex expressions may need manual fixing)

    # ManimGL set_backstroke -> not available in ManimCE, remove it
    code = re.sub(r"\.\s*set_backstroke\s*\([^)]*\)", "", code)

    # ManimGL set_gloss / set_shadow -> not available in ManimCE, remove
    code = re.sub(r"\.\s*set_gloss\s*\([^)]*\)", "", code)
    code = re.sub(r"\.\s*set_shadow\s*\([^)]*\)", "", code)

    # ManimGL DotCloud / GlowDot -> Dot
    code = re.sub(r"\bDotCloud\(", "Dot(", code)
    code = re.sub(r"\bGlowDot\(", "Dot(", code)

    # ManimGL axes.get_graph -> ManimCE axes.plot
    code = re.sub(r"\.get_graph\(", ".plot(", code)

    # CYAN doesn't exist in ManimCE — replace with TEAL
    code = re.sub(r"\bCYAN\b", "TEAL", code)

    # .set_rate_func() is an Animation method, not a mobject method — remove it
    code = re.sub(r"\.\s*set_rate_func\s*\([^)]*\)", "", code)

    if "class GeneratedScene" not in code:
        code = re.sub(
            r"class (\w+)\((Scene|ThreeDScene|MovingCameraScene)\)",
            r"class GeneratedScene(\2)",
            code,
            count=1,
        )

    # Strip any remaining markdown artifacts (triple backticks, control chars)
    code = re.sub(r"^```\w*\s*$", "", code, flags=re.MULTILINE)
    code = re.sub(r"^```\s*$", "", code, flags=re.MULTILINE)
    code = re.sub(r"[^\x09\x0a\x0d\x20-\x7e\x80-\uffff]", "", code)

    # Remove lines that are clearly not Python (common model artifacts)
    cleaned_lines = []
    for line in code.split("\n"):
        stripped = line.strip()
        if stripped in ("```python", "```", "```py"):
            continue
        cleaned_lines.append(line)
    code = "\n".join(cleaned_lines)

    # Strip trailing non-code text after the class definition ends
    # (e.g. "# API Check Notes:", "[3Blue1BrownQuality]", explanations)
    lines = code.split("\n")
    last_code_line = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        # Check if this line is inside the class (indented) or is import/class declaration
        if lines[i].startswith((" ", "\t")) or lines[i].startswith("from ") or lines[i].startswith("import ") or lines[i].startswith("class "):
            last_code_line = i + 1
            break
        # Non-indented, non-import, non-class line after class = trailing text
        last_code_line = i
        break
    code = "\n".join(lines[:last_code_line])

    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        logger.warning("Generated code has syntax error: %s", e)
        # Try to extract just from 'from manim' to end of the last indented block
        class_match = re.search(
            r"(from manim import \*.*?class GeneratedScene\([^)]*\):.*)",
            code, re.S,
        )
        if class_match:
            candidate = class_match.group(1)
            # Trim trailing non-code lines
            cand_lines = candidate.split("\n")
            while cand_lines and not cand_lines[-1].strip():
                cand_lines.pop()
            while cand_lines and not (cand_lines[-1].startswith((" ", "\t")) or cand_lines[-1].startswith("from ") or cand_lines[-1].startswith("class ")):
                cand_lines.pop()
            candidate = "\n".join(cand_lines)
            try:
                compile(candidate, "<generated>", "exec")
                logger.info("Recovered valid code by extracting class definition")
                code = candidate
            except SyntaxError:
                pass

    return code


def _extract_categories(text: str) -> list[str]:
    """Extract category list from model's JSON response."""
    text = text.strip()

    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
        cats = data.get("requested_categories", [])
        if isinstance(cats, list):
            return cats[:4]
    except json.JSONDecodeError:
        pass

    import re as re_mod
    matches = re_mod.findall(r'"([a-z_]+)"', text)
    known_cats = {
        "equations_and_tex", "graphs_and_plots", "3d_visualization",
        "geometric_proofs", "coordinate_systems", "number_animations",
        "complex_analysis", "text_and_typography", "physics_simulations",
        "character_animations",
    }
    return [m for m in matches if m in known_cats][:4]


def generate_scene_code(
    scene: dict,
    plan: dict,
    context: Context,
) -> str:
    """Generate ManimCE code for a single scene.

    Args:
        scene: Scene dict from the plan.
        plan: Full plan dict for context.
        context: Processed context.

    Returns:
        Validated Python code string.
    """
    scene_id = scene.get("id", "unknown")
    logger.info("Codegen: starting scene %s - %s", scene_id, scene.get("title", ""))
    t0 = time.time()

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    # Golden examples disabled — relying on skills + API docs in system prompt
    example_code = "# Refer to the API reference and best practices in the system prompt."

    # Generate the scene code
    refs_str = json.dumps(scene.get("references", []), indent=2)
    scene_prompt = CODEGEN_SCENE_PROMPT.format(
        scene_title=scene.get("title", ""),
        narration=scene.get("narration", ""),
        visual_description=scene.get("visual_description", ""),
        manim_approach=scene.get("manim_approach", ""),
        estimated_duration=scene.get("estimated_duration", 30),
        aspect_ratio=scene.get("aspect_ratio", "16:9"),
        layout_hint=scene.get("layout_hint", "Standard 16:9 landscape layout."),
        style_hint=scene.get("style_hint",
                             "Moderately paced. run_time=1.5-2.5s for animations. "
                             "self.wait(1.5-3) after key moments."),
        references=refs_str,
        example_code=example_code,
    )

    try:
        code_response = _api_call_with_retry(
            client, CODEGEN_MODEL,
            contents=[
                Content(role="model", parts=[Part(text=CODEGEN_SYSTEM_PROMPT)]),
                Content(role="user", parts=[Part(text=scene_prompt)]),
            ],
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=THINKING_BUDGET),
                tools=[Tool(google_search=GoogleSearch())],
            ),
            scene_id=scene_id, call_label="codegen",
        )

        code_text = ""
        if code_response.candidates and code_response.candidates[0].content:
            for part in code_response.candidates[0].content.parts:
                if part.text:
                    code_text += part.text

    except Exception as e:
        logger.error("Codegen %s: generation failed: %s", scene_id, e)
        raise RuntimeError(f"Code generation failed for {scene_id}: {e}") from e

    code = _extract_code(code_text)
    code = _validate_code(code)

    elapsed = time.time() - t0
    logger.info("Codegen %s: completed in %.1fs (%d chars)", scene_id, elapsed, len(code))
    return code


def recover_from_error(
    scene_id: str,
    failed_code: str,
    error_traceback: str,
) -> str:
    """Attempt to fix code that failed to render.

    Args:
        scene_id: Scene identifier for logging.
        failed_code: The code that failed.
        error_traceback: The error output from the renderer.

    Returns:
        Fixed Python code string.
    """
    logger.info("Codegen %s: attempting error recovery", scene_id)

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    prompt = ERROR_RECOVERY_PROMPT.format(
        error_traceback=error_traceback,
        failed_code=failed_code,
    )

    response = _api_call_with_retry(
        client, CODEGEN_MODEL_FAST,
        contents=[
            Content(role="model", parts=[Part(text=CODEGEN_SYSTEM_PROMPT)]),
            Content(role="user", parts=[Part(text=prompt)]),
        ],
        config=GenerateContentConfig(
            thinking_config=ThinkingConfig(thinking_budget=THINKING_BUDGET),
            tools=[Tool(google_search=GoogleSearch())],
        ),
        scene_id=scene_id, call_label="error-recovery",
    )

    code_text = ""
    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.text:
                code_text += part.text

    code = _extract_code(code_text)
    code = _validate_code(code)
    return code


def edit_scene_code(current_code: str, edit_instruction: str) -> str:
    """Edit an existing scene based on teacher's instruction.

    Args:
        current_code: The current scene Python code.
        edit_instruction: What the teacher wants changed.

    Returns:
        Modified Python code string.
    """
    logger.info("Codegen: editing scene with instruction: %s", edit_instruction[:100])

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    prompt = EDIT_PROMPT.format(
        current_code=current_code,
        edit_instruction=edit_instruction,
    )

    response = _api_call_with_retry(
        client, CODEGEN_MODEL_FAST,
        contents=[
            Content(role="model", parts=[Part(text=CODEGEN_SYSTEM_PROMPT)]),
            Content(role="user", parts=[Part(text=prompt)]),
        ],
        config=GenerateContentConfig(
            thinking_config=ThinkingConfig(thinking_budget=THINKING_BUDGET),
            tools=[Tool(google_search=GoogleSearch())],
        ),
        scene_id="edit", call_label="edit",
    )

    code_text = ""
    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.text:
                code_text += part.text

    code = _extract_code(code_text)
    code = _validate_code(code)
    return code


def generate_all_scenes(
    plan: dict,
    context: Context,
    output_dir: Path | None = None,
) -> dict[str, str]:
    """Generate code for all scenes in parallel.

    Each scene's code is written to disk as soon as it's ready so partial
    results survive crashes.

    Args:
        plan: Scene plan dict.
        context: Processed context.
        output_dir: If provided, each scene is saved to ``output_dir/{scene_id}.py``
                    immediately upon completion.

    Returns:
        Dict mapping scene_id to Python code.
    """
    scenes = plan.get("scenes", [])
    if not scenes:
        return {}

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Codegen: generating %d scenes in parallel (max %d workers)", len(scenes), MAX_PARALLEL_SCENES)
    t0 = time.time()

    results: dict[str, str] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SCENES) as pool:
        futures = {
            pool.submit(generate_scene_code, scene, plan, context): scene["id"]
            for scene in scenes
        }
        for future in as_completed(futures):
            scene_id = futures[future]
            try:
                code = future.result()
                results[scene_id] = code
                if output_dir is not None:
                    (output_dir / f"{scene_id}.py").write_text(code, encoding="utf-8")
                    logger.info("Codegen %s: saved to %s", scene_id, output_dir / f"{scene_id}.py")
            except Exception as e:
                errors.append(f"Scene {scene_id}: {e}")
                logger.error("Codegen failed for %s: %s", scene_id, e)

    elapsed = time.time() - t0
    logger.info("Codegen: %d/%d scenes completed in %.1fs", len(results), len(scenes), elapsed)

    if errors:
        logger.warning("Codegen errors: %s", "; ".join(errors))

    return results
