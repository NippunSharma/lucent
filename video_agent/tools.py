import json
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google import genai
from google.genai import types
from google.genai.types import GenerateContentConfig, ThinkingConfig, Tool, GoogleSearch

BASE_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

_sse_callback = None
_status_lock = threading.Lock()


def set_sse_callback(fn):
    """Register a callback to broadcast SSE events when status changes.

    Called by server.py at startup to wire _broadcast_sse into tools.
    """
    global _sse_callback
    _sse_callback = fn


PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
TTS_LOCATION = os.environ.get("GOOGLE_CLOUD_TTS_LOCATION", "us-central1")
TTS_MODEL = "gemini-2.5-flash-tts"
TTS_VOICE = "Algenib"
TTS_PROMPT_STYLE = (
    "Narrate in a warm, friendly, and engaging teaching tone, "
    "as if explaining to a curious student. Speak clearly and at a moderate pace."
)
TTS_MAX_RETRIES = 3


def _update_status(video_id: str, status: str, step: str, **kwargs):
    status_data = {"status": status, "step": step, "timestamp": time.time(), **kwargs}
    status_path = BASE_OUTPUT_DIR / video_id / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    with _status_lock:
        with open(status_path, "w") as f:
            json.dump(status_data, f, indent=2)
    if _sse_callback:
        try:
            _sse_callback(video_id, status_data)
        except Exception:
            pass


def _write_wav(filename: str, pcm_data: bytes, sample_rate=24000, channels=1, sample_width=2):
    data_size = len(pcm_data)
    with open(filename, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))
        f.write(struct.pack("<H", channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", sample_rate * channels * sample_width))
        f.write(struct.pack("<H", channels * sample_width))
        f.write(struct.pack("<H", sample_width * 8))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm_data)


# ---------------------------------------------------------------------------
# Prompt Enhancement — web-grounded research
# ---------------------------------------------------------------------------

RESEARCH_PROMPT = r"""You are a research assistant preparing material for an animated video.
Given a topic, produce a thorough research brief that will be used to plan an animated explainer video.

OUTPUT FORMAT (strict JSON):
{
  "topic_title": "A clear, engaging title for this topic",
  "summary": "2-3 sentence executive summary of the topic",
  "subtopics": [
    {
      "title": "Subtopic name",
      "key_points": ["fact 1", "fact 2", "fact 3"],
      "statistics": ["stat with source if available"],
      "visual_metaphor": "A visual way to explain this concept",
      "depth": "brief | moderate | detailed"
    }
  ],
  "hook": "An opening fact, question, or statement to grab attention",
  "common_misconceptions": ["misconception 1", "misconception 2"],
  "real_world_applications": ["application 1", "application 2"],
  "memorable_takeaway": "A closing thought that stays with the viewer",
  "suggested_narrative_arc": "Brief description of how to structure the story"
}

REQUIREMENTS:
- Include subtopics covering the topic comprehensively
- Each subtopic should have 3-5 key points with verified facts
- Include at least 5 statistics or specific numbers
- Suggest visual metaphors that can be animated
- The hook should be surprising or thought-provoking
- Use web search results to verify facts and find recent information

Output ONLY valid JSON. No markdown fences, no extra text."""


def enhance_prompt(topic: str) -> dict:
    """Expand a short topic into a rich, web-grounded research brief.

    Uses Gemini with Google Search grounding to pull real, verified
    information about the topic. Checks cache first.

    Args:
        topic: The user's topic (e.g. "How does photosynthesis work?")

    Returns:
        dict with status and research_brief (the full JSON brief).
    """
    from .cache import get_cached_research, cache_research

    cached = get_cached_research(topic)
    if cached:
        print(f"  [RESEARCH] Cache hit for topic: '{topic}'")
        return {"status": "success", "research_brief": cached, "cached": True}

    print(f"  [RESEARCH] Enhancing prompt: '{topic}'")
    t0 = time.time()

    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=f"{RESEARCH_PROMPT}\n\nTOPIC: {topic}",
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=8000),
                tools=[Tool(google_search=GoogleSearch())],
            ),
        )
    except Exception as e:
        print(f"  [RESEARCH] Grounded search failed: {e}, falling back to ungrounded")
        try:
            response = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=f"{RESEARCH_PROMPT}\n\nTOPIC: {topic}",
                config=GenerateContentConfig(
                    thinking_config=ThinkingConfig(thinking_budget=8000),
                ),
            )
        except Exception as e2:
            print(f"  [RESEARCH] Fallback also failed: {e2}")
            return {
                "status": "success",
                "research_brief": {"raw_research": "", "topic_title": topic},
                "error": f"Research unavailable: {e2}",
            }

    if not response.candidates or not response.candidates[0].content or not response.candidates[0].content.parts:
        return {"status": "error", "error_message": "Gemini returned no candidates (safety filter or quota)."}

    raw_text = ""
    for part in response.candidates[0].content.parts:
        if part.text:
            raw_text += part.text

    clean = raw_text.strip()
    if clean.startswith("```"):
        import re
        fence = re.search(r"```(?:json)?\s*\n(.*?)```", clean, re.S)
        if fence:
            clean = fence.group(1).strip()

    try:
        brief = json.loads(clean)
    except json.JSONDecodeError:
        brief = {"raw_research": raw_text, "topic_title": topic}

    elapsed = time.time() - t0
    print(f"  [RESEARCH] Research brief generated in {elapsed:.1f}s")

    cache_research(topic, brief)

    return {
        "status": "success",
        "research_brief": brief,
        "elapsed_seconds": round(elapsed, 1),
    }


# ---------------------------------------------------------------------------
# TTS Generation
# ---------------------------------------------------------------------------

def _generate_single_tts(client, sid: str, narration: str, audio_dir: Path) -> tuple[str, float]:
    """Generate TTS for a single section with retry logic."""
    last_err = None
    for attempt in range(1, TTS_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=TTS_MODEL,
                contents=f"{TTS_PROMPT_STYLE}: {narration}",
                config=types.GenerateContentConfig(
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=TTS_VOICE,
                            )
                        )
                    ),
                ),
            )

            if (
                not response.candidates
                or not response.candidates[0].content
                or not response.candidates[0].content.parts
                or not response.candidates[0].content.parts[0].inline_data
            ):
                raise RuntimeError("TTS API returned no audio data (empty candidates or missing inline_data)")
            pcm_data = response.candidates[0].content.parts[0].inline_data.data
            wav_path = str(audio_dir / f"{sid}.wav")
            _write_wav(wav_path, pcm_data)

            duration = len(pcm_data) / (24000 * 2)
            print(f"    [TTS] {sid}: saved ({duration:.1f}s)")
            return sid, round(duration, 2)
        except Exception as e:
            last_err = e
            if attempt < TTS_MAX_RETRIES:
                wait = 2 ** attempt
                print(f"    [TTS] {sid}: attempt {attempt} failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
    if last_err:
        raise last_err
    raise RuntimeError(f"TTS generation failed for '{sid}' after all retries")


def generate_tts_audio(video_id: str, sections_json: str) -> dict:
    """Generate TTS audio files for each scene's narration in parallel.

    Args:
        video_id: Unique identifier for this video project.
        sections_json: JSON array of {id, narration} objects.

    Returns:
        dict with status, section_durations, total_duration.
    """
    try:
        sections = json.loads(sections_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid JSON: {e}"}

    _update_status(video_id, "processing", "generating_audio")

    audio_dir = BASE_OUTPUT_DIR / video_id / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    client = genai.Client(vertexai=True, project=PROJECT_ID, location=TTS_LOCATION)
    section_durations = {}
    errors = []

    valid_sections = [s for s in sections if s.get("id") and s.get("narration")]
    if not valid_sections:
        return {"status": "success", "section_durations": {}, "total_duration": 0, "message": "No sections to generate."}

    print(f"  [TTS] Generating {len(valid_sections)} sections in parallel...")
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=max(1, min(len(valid_sections), 8))) as pool:
        futures = {
            pool.submit(_generate_single_tts, client, sec["id"], sec["narration"], audio_dir): sec["id"]
            for sec in valid_sections
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                section_id, duration = future.result()
                section_durations[section_id] = duration
            except Exception as e:
                errors.append(f"TTS failed for '{sid}': {e}")

    elapsed = time.time() - t0

    if errors:
        return {"status": "error", "error_message": "; ".join(errors)}

    total = sum(section_durations.values())
    print(f"  [TTS] All {len(sections)} sections done in {elapsed:.1f}s (total audio: {total:.1f}s)")
    return {
        "status": "success",
        "section_durations": section_durations,
        "total_duration": round(total, 2),
        "message": f"Generated audio for {len(sections)} sections ({total:.1f}s total) in {elapsed:.1f}s",
    }


REMOTION_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "remotion_project" / "public"


# ---------------------------------------------------------------------------
# TTS-Only Asset Pipeline (image generation disabled — all visuals are code-driven)
# ---------------------------------------------------------------------------

def generate_tts_only(video_id: str, plan_json: str) -> dict:
    """Generate TTS audio from the orchestrator's plan. No image generation.

    All visuals are code-driven (SVG, CSS, React components).
    Only user-provided reference images are passed through.

    Args:
        video_id: Unique identifier for this video project.
        plan_json: The orchestrator's plan JSON string containing scenes.

    Returns:
        dict with status, audio results, and updated plan.
    """
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "error_message": f"Invalid plan JSON: {e}"}

    scenes = plan.get("scenes", [])
    if not scenes:
        return {"status": "error", "error_message": "No scenes in plan."}

    project_dir = BASE_OUTPUT_DIR / video_id
    project_dir.mkdir(parents=True, exist_ok=True)
    with open(project_dir / "plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)

    _update_status(video_id, "processing", "generating_audio")

    tts_sections = []
    for sc in scenes:
        if sc.get("narration"):
            tts_sections.append({"id": sc["id"], "narration": sc["narration"]})

    print(f"  [ASSETS] Generating: {len(tts_sections)} TTS sections (no image generation — code-driven visuals)")
    t0 = time.time()

    audio_result = None
    if tts_sections:
        audio_result = generate_tts_audio(video_id, json.dumps(tts_sections))

    elapsed = time.time() - t0

    if audio_result and audio_result.get("section_durations"):
        for sc in scenes:
            dur = audio_result["section_durations"].get(sc["id"])
            if dur:
                sc["audio_duration"] = dur

    # Collect user-provided reference images (no generation — just pass through)
    ref_images = []
    for sc in scenes:
        for asset in sc.get("assets", []):
            fname = asset.get("filename", "")
            if fname and not asset.get("prompt") and not asset.get("tool"):
                ref_path = REMOTION_PUBLIC_DIR / fname
                if ref_path.exists():
                    ref_images.append({"filename": fname, "path": str(ref_path), "type": "user_provided"})

    plan["scenes"] = scenes
    with open(project_dir / "plan_with_assets.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)

    combined = {
        "status": "success",
        "audio": audio_result or {},
        "reference_images": ref_images,
        "updated_plan": plan,
        "elapsed_seconds": round(elapsed, 1),
        "message": f"Generated {len(tts_sections)} TTS sections in {elapsed:.1f}s. All visuals are code-driven.",
    }

    if audio_result and audio_result.get("status") == "error":
        combined["status"] = "partial"

    print(f"  [ASSETS] Done in {elapsed:.1f}s: {combined['message']}")
    return combined
