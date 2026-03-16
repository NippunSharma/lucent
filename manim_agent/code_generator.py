"""ManimGL code generation with two-turn self-selecting examples and error recovery.

Turn 1: Model sees the catalog and picks relevant categories.
Turn 2: Model receives actual example code and generates the scene.
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
from .golden_examples import get_catalog_prompt, load_examples_for_categories
from .prompt_templates import (
    CODEGEN_CATEGORY_REQUEST_PROMPT,
    CODEGEN_SCENE_PROMPT,
    CODEGEN_SYSTEM_PROMPT,
    EDIT_PROMPT,
    ERROR_RECOVERY_PROMPT,
)

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
CODEGEN_MODEL = "gemini-3-flash-preview"
CODEGEN_MODEL_FAST = "gemini-3-flash-preview"
THINKING_BUDGET = 32000
MAX_RETRIES = 3
MAX_PARALLEL_SCENES = 2
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
    """Validate and fix common issues in generated code."""
    if "from manimlib import" not in code:
        code = "from manimlib import *\n\n" + code

    code = re.sub(r"from manim import.*\n?", "", code)
    code = re.sub(r"MathTex\(", "Tex(", code)
    code = re.sub(r"self\.embed\(\).*\n?", "", code)

    # Fix ManimCE -> ManimGL API name mismatches
    code = re.sub(r"\bCreate\(", "ShowCreation(", code)
    code = re.sub(r"\bUncreate\b", "Uncreate", code)  # Uncreate exists, keep it
    code = re.sub(r"\bDot3D\(", "Sphere(radius=0.08, ", code)
    code = re.sub(r"\bself\.move_camera\(", "self.camera.frame.set_euler_angles(", code)

    # ManimCE fix_in_frame -> ManimGL pattern
    code = re.sub(r"self\.add_fixed_in_frame_mobjects\(([^)]+)\)", r"\1.fix_in_frame()\n        self.add(\1)", code)

    # self.frame -> self.camera.frame (in plain Scene, not InteractiveScene)
    # Only fix if the class inherits from Scene (not InteractiveScene)
    if "InteractiveScene" not in code:
        code = re.sub(r"\bself\.frame\b", "self.camera.frame", code)

    # VGroup(*self.mobjects) crashes when scene has non-VMobjects — use Group instead
    code = re.sub(r"VGroup\(\s*\*\s*self\.mobjects\s*\)", "Group(*self.mobjects)", code)

    # Fix reorient() with named kwargs — convert to positional args
    code = re.sub(
        r"\.reorient\(\s*theta\s*=\s*([^,)]+)\s*,\s*phi\s*=\s*([^,)]+)\s*\)",
        r".reorient(\1, \2)", code,
    )
    code = re.sub(
        r"\.reorient\(\s*phi\s*=\s*([^,)]+)\s*,\s*theta\s*=\s*([^,)]+)\s*\)",
        r".reorient(\2, \1)", code,
    )
    code = re.sub(
        r"\.reorient\(\s*phi\s*=\s*([^,)]+)\s*\)",
        r".reorient(0, \1)", code,
    )
    code = re.sub(
        r"\.reorient\(\s*theta\s*=\s*([^,)]+)\s*\)",
        r".reorient(\1)", code,
    )

    # .set_fill() doesn't exist on 3D objects (Cube, Prism, Sphere, etc.)
    # Replace chained .set_fill(color, opacity) with .set_color(color, opacity)
    code = re.sub(
        r"\.(Cube|Prism|Sphere|Torus|Cylinder|Cone)\(([^)]*)\)\s*\.\s*set_fill\(",
        r".\1(\2).set_color(", code,
    )

    if "class GeneratedScene" not in code:
        code = re.sub(
            r"class (\w+)\((Scene|ThreeDScene)\)",
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
        # Try to extract just from 'from manimlib' to end of the last indented block
        class_match = re.search(
            r"(from manimlib import \*.*?class GeneratedScene\([^)]*\):.*)",
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
    """Generate ManimGL code for a single scene using two-turn conversation.

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

    catalog_prompt = get_catalog_prompt()
    category_request = CODEGEN_CATEGORY_REQUEST_PROMPT.format(
        catalog=catalog_prompt,
        scene_title=scene.get("title", ""),
        visual_description=scene.get("visual_description", ""),
        manim_approach=scene.get("manim_approach", ""),
    )

    # Turn 1: Ask model which categories it wants
    try:
        cat_response = _api_call_with_retry(
            client, CODEGEN_MODEL,
            contents=[
                Content(role="model", parts=[Part(text=CODEGEN_SYSTEM_PROMPT)]),
                Content(role="user", parts=[Part(text=category_request)]),
            ],
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=4000),
            ),
            scene_id=scene_id, call_label="categories",
        )

        cat_text = ""
        if cat_response.candidates and cat_response.candidates[0].content:
            for part in cat_response.candidates[0].content.parts:
                if part.text:
                    cat_text += part.text

        categories = _extract_categories(cat_text)
        logger.info("Codegen %s: requested categories: %s", scene_id, categories)

    except Exception as e:
        logger.warning("Codegen %s: category selection failed (%s), using defaults", scene_id, e)
        categories = ["equations_and_tex", "text_and_typography"]

    # Load example code for selected categories
    if categories:
        example_code = load_examples_for_categories(categories)
    else:
        example_code = "# No example code requested"

    # Turn 2: Generate the scene code
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
