"""TTS audio generation using Gemini 2.5 Flash TTS.

Extracted from video_agent/tools.py and adapted for the Manim pipeline.
Generates WAV files for each scene's narration in parallel.
"""

import logging
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")
TTS_LOCATION = os.environ.get("GOOGLE_CLOUD_TTS_LOCATION", "us-central1")
TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-2.5-flash-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "Algenib")
TTS_PROMPT_STYLE = (
    "Narrate in a warm, friendly, and engaging teaching tone, "
    "as if explaining to a curious student. Speak clearly and at a moderate pace."
)
TTS_MAX_RETRIES = 3


def _write_wav(
    filename: str,
    pcm_data: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
) -> None:
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


def _generate_single_tts(
    client: genai.Client,
    scene_id: str,
    narration: str,
    audio_dir: Path,
) -> tuple[str, float]:
    """Generate TTS for a single scene with retry logic."""
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
                raise RuntimeError("TTS API returned no audio data")

            pcm_data = response.candidates[0].content.parts[0].inline_data.data
            wav_path = str(audio_dir / f"{scene_id}.wav")
            _write_wav(wav_path, pcm_data)

            duration = len(pcm_data) / (24000 * 2)
            logger.info("TTS %s: saved (%.1fs)", scene_id, duration)
            return scene_id, round(duration, 2)
        except Exception as e:
            last_err = e
            if attempt < TTS_MAX_RETRIES:
                wait = 2**attempt
                logger.warning(
                    "TTS %s: attempt %d failed (%s), retrying in %ds...",
                    scene_id, attempt, e, wait,
                )
                time.sleep(wait)

    raise last_err or RuntimeError(f"TTS failed for '{scene_id}' after all retries")


def generate_tts_for_scenes(
    scenes: list[dict],
    audio_dir: Path,
) -> dict[str, float]:
    """Generate TTS audio for all scenes in parallel.

    Args:
        scenes: List of dicts with at least ``id`` and ``narration`` keys.
        audio_dir: Directory to write .wav files into.

    Returns:
        Mapping of ``{scene_id: duration_seconds}``.

    Raises:
        RuntimeError: If any scene's TTS generation fails after all retries.
    """
    audio_dir.mkdir(parents=True, exist_ok=True)

    valid = [s for s in scenes if s.get("id") and s.get("narration")]
    if not valid:
        logger.info("TTS: no scenes with narration, skipping")
        return {}

    client = genai.Client(vertexai=True, project=PROJECT_ID, location=TTS_LOCATION)
    durations: dict[str, float] = {}
    errors: list[str] = []

    logger.info("TTS: generating %d scenes in parallel...", len(valid))
    t0 = time.time()

    workers = max(1, min(len(valid), 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_generate_single_tts, client, s["id"], s["narration"], audio_dir): s["id"]
            for s in valid
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                scene_id, dur = future.result()
                durations[scene_id] = dur
            except Exception as e:
                errors.append(f"TTS failed for '{sid}': {e}")

    elapsed = time.time() - t0

    if errors:
        raise RuntimeError("; ".join(errors))

    total = sum(durations.values())
    logger.info(
        "TTS: all %d scenes done in %.1fs (total audio: %.1fs)",
        len(valid), elapsed, total,
    )
    return durations
