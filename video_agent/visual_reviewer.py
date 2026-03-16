"""Visual review — now integrated into code_generator.py.

This module is kept for backward compatibility. The review logic now lives
inside the per-scene conversation loop in code_generator.generate_composition().

review_composition() is no longer needed as a separate pipeline step — it is
called automatically at the end of generate_composition().
"""


def review_composition(video_id: str, composition_json: str) -> dict:
    """No-op: visual review is now integrated into generate_composition().

    Kept for backward compatibility — returns a message explaining the change.
    """
    return {
        "status": "skipped",
        "message": (
            "Visual review is now integrated into generate_composition(). "
            "It runs automatically as part of the code generation pipeline."
        ),
    }
