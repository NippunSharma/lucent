import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage

from google import genai
from google.genai.types import (
    GenerateContentConfig,
    GenerateImagesConfig,
    ImageConfig,
    Modality,
    Part,
    Blob,
)

_MAX_IMAGE_WIDTH = 1920

_IMAGEN_ASPECT_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9"}
_NANO_ASPECT_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}

_ASPECT_RATIO_VALUES = {
    "1:1": 1.0, "5:4": 1.25, "4:3": 1.333, "3:2": 1.5,
    "16:9": 1.778, "21:9": 2.333,
    "4:5": 0.8, "3:4": 0.75, "2:3": 0.667, "9:16": 0.5625,
}


def derive_aspect_ratio(width: int, height: int, model: str = "imagen") -> str:
    """Pick the closest supported aspect ratio for the given dimensions."""
    target = width / height if height else 1.0
    valid = _IMAGEN_ASPECT_RATIOS if model == "imagen" else _NANO_ASPECT_RATIOS
    best, best_diff = "16:9", float("inf")
    for label, value in _ASPECT_RATIO_VALUES.items():
        if label not in valid:
            continue
        diff = abs(value - target)
        if diff < best_diff:
            best_diff = diff
            best = label
    return best


_NON_RETRYABLE_KEYWORDS = [
    "authentication", "403", "401", "permission", "not found",
    "safety", "blocked", "prohibited", "invalid model",
]


def _is_retryable_error(error_message: str) -> bool:
    lower = error_message.lower()
    return not any(kw in lower for kw in _NON_RETRYABLE_KEYWORDS)
_JPEG_QUALITY = 70

BASE_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

IMAGE_MODEL = "gemini-3.1-flash-image-preview"
IMAGEN_MODEL = "imagen-4.0-generate-001"

# --- Backend configuration ---
# Nano Banana (Gemini Flash Image): defaults to AI Studio
# Set NANO_BANANA_BACKEND=vertexai to switch
NANO_BANANA_BACKEND = os.environ.get("NANO_BANANA_BACKEND", "aistudio").lower()

# Imagen 4: defaults to Vertex AI
# Set IMAGEN_BACKEND=aistudio to switch
IMAGEN_BACKEND = os.environ.get("IMAGEN_BACKEND", "vertexai").lower()

# Vertex AI settings
_VERTEX_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
_VERTEX_LOCATION = "global"
_IMAGEN_VERTEX_LOCATION = os.environ.get("IMAGEN_VERTEX_LOCATION", "us-central1")

# AI Studio settings
_AISTUDIO_API_KEY = os.environ.get("GOOGLE_AI_STUDIO_API_KEY", "")


def _get_nano_banana_client() -> genai.Client:
    """Client for Nano Banana (Gemini Flash Image). Defaults to AI Studio."""
    if NANO_BANANA_BACKEND == "vertexai":
        return genai.Client(
            vertexai=True, project=_VERTEX_PROJECT, location=_VERTEX_LOCATION
        )
    return genai.Client(vertexai=False, api_key=_AISTUDIO_API_KEY)


def _get_imagen_client() -> genai.Client:
    """Client for Imagen 4. Defaults to Vertex AI."""
    if IMAGEN_BACKEND == "aistudio":
        return genai.Client(vertexai=False, api_key=_AISTUDIO_API_KEY)
    return genai.Client(
        vertexai=True, project=_VERTEX_PROJECT, location=_IMAGEN_VERTEX_LOCATION
    )


def _guess_mime(filepath: str) -> str:
    lower = filepath.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/png"


def _ext_from_mime(mime: str) -> str:
    if "jpeg" in mime or "jpg" in mime:
        return ".jpg"
    if "webp" in mime:
        return ".webp"
    if "gif" in mime:
        return ".gif"
    return ".png"


def _compress_image(raw_bytes: bytes, output_path: Path) -> tuple[Path, int]:
    """Resize to max 1920px width and save as compressed JPEG.

    Returns (output_path, file_size_bytes).
    """
    buf = BytesIO(raw_bytes)
    img = PILImage.open(buf)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    w, h = img.size
    if w > _MAX_IMAGE_WIDTH:
        ratio = _MAX_IMAGE_WIDTH / w
        resized = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
        img.close()
        img = resized

    out = output_path.with_suffix(".jpg")
    img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    img.close()
    buf.close()
    return out, out.stat().st_size


def generate_image(
    video_id: str,
    prompt: str,
    reference_image_path: str = "",
    output_filename: str = "",
    aspect_ratio: str = "16:9",
) -> dict:
    """Generate or edit an image using Gemini 3.1 Flash Image (Nano Banana).

    Supports three modes:
      1. Text-to-image — provide only a prompt.
      2. Image editing — provide a prompt + reference_image_path to edit/modify.
      3. Style reference — provide a reference image for visual consistency
         and describe the new image in the prompt.

    Generated images are saved to output/{video_id}/images/ and can be used by:
      - Manim: ImageMobject("absolute/path/to/image.png")
      - Remotion: <Img src={staticFile('filename.png')} /> (images are
        automatically copied to the Remotion public/ folder before rendering)

    Args:
        video_id: Unique identifier for this video project.
        prompt: Detailed text description of the image to generate or the
            editing instruction. Be specific about subject, composition,
            style, colors, lighting, mood, and aspect ratio.
            Examples:
              "A flat-vector illustration of a friendly robot teacher pointing
               at a blackboard, dark blue background, 16:9 aspect ratio"
              "Edit this image: change the background to a starry night sky"
              "Generate an image in the same art style as the reference:
               a medieval castle on a hilltop at sunset"
        reference_image_path: Optional absolute path to a reference image.
            Used for image editing or maintaining visual consistency.
            Leave empty for pure text-to-image generation.
        output_filename: Optional filename for the output (e.g., "bg_intro.png").
            If empty, a unique name is generated automatically.

    Returns:
        dict with:
          - status: "success" or "error"
          - image_path: absolute path to the saved image
          - image_filename: just the filename (for Remotion staticFile)
          - message: human-readable description
    """
    if not prompt or not prompt.strip():
        return {"status": "error", "error_message": "Empty prompt — cannot generate image."}

    images_dir = BASE_OUTPUT_DIR / video_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    client = _get_nano_banana_client()

    # Build contents: reference image (optional) + text prompt
    if reference_image_path and os.path.exists(reference_image_path):
        with open(reference_image_path, "rb") as f:
            img_data = f.read()
        mime = _guess_mime(reference_image_path)
        contents = [
            Part(inline_data=Blob(data=img_data, mime_type=mime)),
            prompt,
        ]
        print(f"  [IMAGE] Generating with reference: {reference_image_path}")
    else:
        contents = prompt
        if reference_image_path:
            print(f"  [IMAGE] Warning: reference not found: {reference_image_path}")
        print(f"  [IMAGE] Generating from text prompt...")

    ar = aspect_ratio if aspect_ratio in _NANO_ASPECT_RATIOS else "16:9"
    print(f"  [IMAGE] Prompt: {prompt[:120]}... (aspect_ratio={ar})")

    try:
        response = client.models.generate_content(
            model=IMAGE_MODEL,
            contents=contents,
            config=GenerateContentConfig(
                response_modalities=[Modality.TEXT, Modality.IMAGE],
                image_config=ImageConfig(aspect_ratio=ar),
            ),
        )
    except Exception as e:
        print(f"  [IMAGE] API error: {e}")
        return {
            "status": "error",
            "error_message": f"Image generation API call failed: {e}",
        }

    if not response.candidates or not response.candidates[0].content.parts:
        print(f"  [IMAGE] No candidates in response")
        return {
            "status": "error",
            "error_message": "No response from image generation model.",
        }

    # Extract generated image(s) — save the first image found
    response_text = ""
    for part in response.candidates[0].content.parts:
        if part.text:
            response_text += part.text
        elif part.inline_data and part.inline_data.data:
            if not output_filename:
                output_filename = f"img_{uuid.uuid4().hex[:8]}.jpg"
            else:
                output_filename = Path(output_filename).stem + ".jpg"

            image_path = images_dir / output_filename
            try:
                compressed_path, file_size = _compress_image(
                    part.inline_data.data, image_path
                )
            except Exception as save_err:
                return {"status": "error", "error_message": f"Image generated but save failed: {save_err}"}
            output_filename = compressed_path.name

            abs_path = str(compressed_path.resolve())
            size_kb = file_size / 1024
            print(f"  [IMAGE] Saved (compressed {size_kb:.0f}KB): {abs_path}")

            return {
                "status": "success",
                "image_path": abs_path,
                "image_filename": output_filename,
                "message": f"Image generated and saved as {output_filename} ({size_kb:.0f}KB)",
                "model_commentary": response_text.strip() if response_text else "",
            }

    print(f"  [IMAGE] Text-only response: {response_text[:200]}")
    return {
        "status": "error",
        "error_message": "Model returned text only, no image was generated.",
        "response_text": response_text[:500],
        "suggestion": (
            "Try rephrasing the prompt to be more specific about the image. "
            "Include details like subject, style, colors, and composition."
        ),
    }


def generate_image_fast(
    video_id: str,
    prompt: str,
    output_filename: str = "",
    aspect_ratio: str = "16:9",
) -> dict:
    """Generate an image using Imagen 4 (fast, high quality, no text/edit support).

    Best for: backgrounds, landscapes, visual scenes, generic illustrations,
    environments, abstract textures, and any image where embedded text quality
    is not critical. Faster and uses less quota than Nano Banana.

    Does NOT support image editing or reference images — use generate_image
    (Nano Banana) for those use cases.

    Args:
        video_id: Unique identifier for this video project.
        prompt: Text description of the image. Be descriptive about subject,
            style, colors, mood. Do NOT include aspect ratio in the prompt —
            it is set automatically to 16:9.
            Examples:
              "A dramatic oil painting of ancient Rome at sunset, columns and
               temples, warm golden light, cinematic composition"
              "Abstract dark gradient texture with subtle blue and purple tones"
        output_filename: Optional filename (e.g., "bg_intro.jpg").

    Returns:
        dict with status, image_path, image_filename, message.
    """
    if not prompt or not prompt.strip():
        return {"status": "error", "error_message": "Empty prompt — cannot generate image."}

    images_dir = BASE_OUTPUT_DIR / video_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    client = _get_imagen_client()
    ar = aspect_ratio if aspect_ratio in _IMAGEN_ASPECT_RATIOS else "16:9"
    print(f"  [IMAGEN] Generating ({IMAGEN_BACKEND}): {prompt[:120]}... (aspect_ratio={ar})")

    try:
        response = client.models.generate_images(
            model=IMAGEN_MODEL,
            prompt=prompt,
            config=GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=ar,
            ),
        )
    except Exception as e:
        print(f"  [IMAGEN] API error: {e}")
        return {
            "status": "error",
            "error_message": f"Imagen API call failed: {e}",
        }

    if not response.generated_images:
        print(f"  [IMAGEN] No images in response")
        return {
            "status": "error",
            "error_message": "Imagen returned no images.",
        }

    img_obj = response.generated_images[0].image
    img_bytes = img_obj.image_bytes

    if not output_filename:
        output_filename = f"img_{uuid.uuid4().hex[:8]}.jpg"
    else:
        output_filename = Path(output_filename).stem + ".jpg"

    image_path = images_dir / output_filename
    try:
        compressed_path, file_size = _compress_image(img_bytes, image_path)
    except Exception as save_err:
        return {"status": "error", "error_message": f"Image generated but save failed: {save_err}"}
    output_filename = compressed_path.name

    abs_path = str(compressed_path.resolve())
    size_kb = file_size / 1024
    print(f"  [IMAGEN] Saved (compressed {size_kb:.0f}KB): {abs_path}")

    return {
        "status": "success",
        "image_path": abs_path,
        "image_filename": output_filename,
        "message": f"Image generated via Imagen and saved as {output_filename} ({size_kb:.0f}KB)",
    }


def _generate_image_fast_with_retry(
    video_id: str, item: dict, index: int
) -> dict:
    """Generate a single Imagen image with retry."""
    if index > 0:
        time.sleep(index * 1.0)

    prompt = (item.get("prompt") or "").strip()
    filename = item.get("output_filename", "")
    aspect_ratio = item.get("aspect_ratio", "16:9")

    if not prompt:
        print(f"  [IMAGEN-BATCH] #{index} skipped: empty prompt (filename={filename})")
        return {"status": "error", "error_message": "Empty prompt — skipped"}

    for attempt in range(1, 4):
        result = generate_image_fast(
            video_id=video_id, prompt=prompt, output_filename=filename,
            aspect_ratio=aspect_ratio,
        )
        if result.get("status") == "success":
            return result

        err = result.get("error_message", "unknown")
        if not _is_retryable_error(err):
            print(f"  [IMAGEN-BATCH] #{index} non-retryable error: {err[:120]}")
            return result
        if attempt < 3:
            wait = 2 ** attempt + 1
            print(f"  [IMAGEN-BATCH] #{index} retry {attempt}: {err[:100]}")
            time.sleep(wait)
        else:
            return result

    return {"status": "error", "error_message": "Exhausted retries."}


def generate_images_batch_fast(video_id: str, prompts_json: str) -> dict:
    """Generate multiple images using Imagen 4 (fast, parallel).

    Use this for batch-generating backgrounds, landscapes, scenes, and other
    visual elements where text rendering quality is not important.
    Faster and cheaper than generate_images_batch (Nano Banana).

    Does NOT support reference images or image editing.

    Args:
        video_id: Unique identifier for this video project.
        prompts_json: JSON array of image request objects. Each object has:
            - prompt (required): Text description of the image.
            - output_filename (optional): Desired filename (e.g., "bg_intro.jpg").
            - aspect_ratio (optional): One of "1:1", "3:4", "4:3", "9:16", "16:9". Defaults to "16:9".
            Example:
            [
              {"prompt": "A vintage map of India, sepia tones", "output_filename": "bg_intro.jpg", "aspect_ratio": "16:9"},
              {"prompt": "A sailing ship at an Indian port, oil painting style", "output_filename": "bg_ship.jpg", "aspect_ratio": "9:16"}
            ]

    Returns:
        dict with status, results, summary, elapsed_seconds.
    """
    try:
        prompts = json.loads(prompts_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid JSON: {e}"}

    if not isinstance(prompts, list) or len(prompts) == 0:
        return {"status": "error", "error_message": "prompts_json must be a non-empty JSON array."}

    n = len(prompts)
    print(f"  [IMAGEN-BATCH] Generating {n} images via Imagen 4...")
    start_time = time.time()

    results = [None] * n
    with ThreadPoolExecutor(max_workers=3) as pool:
        future_to_idx = {
            pool.submit(_generate_image_fast_with_retry, video_id, prompts[i], i): i
            for i in range(n)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = {"status": "error", "error_message": str(e)}

    elapsed = time.time() - start_time
    succeeded = sum(1 for r in results if r and r.get("status") == "success")
    print(f"  [IMAGEN-BATCH] Done: {succeeded}/{n} succeeded in {elapsed:.1f}s")

    overall = "success" if succeeded == n else ("partial" if succeeded > 0 else "error")
    return {
        "status": overall,
        "results": results,
        "summary": f"{succeeded}/{n} images generated via Imagen in {elapsed:.1f}s",
        "elapsed_seconds": round(elapsed, 1),
    }


_IMAGE_MAX_RETRIES = 3
_IMAGE_CONCURRENCY = 2
_IMAGE_STAGGER_DELAY = 2.0


def _generate_image_with_retry(
    video_id: str, item: dict, index: int
) -> dict:
    """Generate a single image with retry + exponential backoff."""
    if index > 0:
        time.sleep(index * _IMAGE_STAGGER_DELAY)

    prompt = (item.get("prompt") or "").strip()
    filename = item.get("output_filename", "")
    ref = item.get("reference_image_path", "")
    aspect_ratio = item.get("aspect_ratio", "16:9")

    if not prompt:
        print(f"  [IMAGE-BATCH] #{index} skipped: empty prompt (filename={filename})")
        return {"status": "error", "error_message": "Empty prompt — skipped"}

    for attempt in range(1, _IMAGE_MAX_RETRIES + 1):
        result = generate_image(
            video_id=video_id,
            prompt=prompt,
            reference_image_path=ref,
            output_filename=filename,
            aspect_ratio=aspect_ratio,
        )
        if result.get("status") == "success":
            return result

        err = result.get("error_message", "unknown error")
        if not _is_retryable_error(err):
            print(f"  [IMAGE-BATCH] #{index} non-retryable error: {err[:120]}")
            return result
        if attempt < _IMAGE_MAX_RETRIES:
            wait = 2 ** attempt + 1
            print(f"  [IMAGE-BATCH] #{index} failed (attempt {attempt}/{_IMAGE_MAX_RETRIES}): {err[:120]}")
            print(f"  [IMAGE-BATCH] #{index} retrying in {wait}s...")
            time.sleep(wait)
        else:
            print(f"  [IMAGE-BATCH] #{index} failed after {_IMAGE_MAX_RETRIES} attempts: {err[:200]}")
            return result

    return {"status": "error", "error_message": "Exhausted retries."}


def generate_images_batch(video_id: str, prompts_json: str) -> dict:
    """Generate multiple images with controlled concurrency and retry.

    Uses limited parallelism (2 concurrent) with staggered starts and
    automatic retry with exponential backoff to handle API rate limits.

    Args:
        video_id: Unique identifier for this video project.
        prompts_json: JSON array of image request objects. Each object has:
            - prompt (required): Detailed text description of the image.
            - output_filename (optional): Desired filename (e.g., "bg_intro.png").
            - reference_image_path (optional): Path to a reference image for
              editing or style consistency.
            - aspect_ratio (optional): One of "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9". Defaults to "16:9".
            Example:
            [
              {"prompt": "A vintage map of India, sepia tones", "output_filename": "bg_intro.png", "aspect_ratio": "16:9"},
              {"prompt": "A British sailing ship at an Indian port, oil painting style", "output_filename": "bg_ship.png", "aspect_ratio": "4:3"},
              {"prompt": "Silhouette of Gandhi walking at sunset, minimalist", "output_filename": "bg_gandhi.png", "aspect_ratio": "9:16"}
            ]

    Returns:
        dict with:
          - status: "success" or "partial" or "error"
          - results: list of per-image results (each has status, image_path, image_filename)
          - summary: human-readable summary
          - elapsed_seconds: total wall-clock time
    """
    try:
        prompts = json.loads(prompts_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid JSON: {e}"}

    if not isinstance(prompts, list) or len(prompts) == 0:
        return {"status": "error", "error_message": "prompts_json must be a non-empty JSON array."}

    n = len(prompts)
    print(f"  [IMAGE-BATCH] Generating {n} images (concurrency={_IMAGE_CONCURRENCY}, retries={_IMAGE_MAX_RETRIES})...")
    start_time = time.time()

    results = [None] * n
    with ThreadPoolExecutor(max_workers=_IMAGE_CONCURRENCY) as pool:
        future_to_idx = {
            pool.submit(_generate_image_with_retry, video_id, prompts[i], i): i
            for i in range(n)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = {"status": "error", "error_message": str(e)}

    elapsed = time.time() - start_time
    succeeded = sum(1 for r in results if r and r.get("status") == "success")
    print(f"  [IMAGE-BATCH] Done: {succeeded}/{n} succeeded in {elapsed:.1f}s")

    overall = "success" if succeeded == n else ("partial" if succeeded > 0 else "error")

    return {
        "status": overall,
        "results": results,
        "summary": f"{succeeded}/{n} images generated in {elapsed:.1f}s",
        "elapsed_seconds": round(elapsed, 1),
    }
