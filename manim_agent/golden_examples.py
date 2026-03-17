"""Golden Example Library — two-phase example loading for codegen.

Phase 1: The catalog (category names + descriptions + summaries) is shown to the
codegen model so it can self-select which categories are relevant.

Phase 2: The actual .py code for the selected categories is loaded and injected
into the codegen prompt.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = Path(__file__).resolve().parent / "example_catalog.json"


def _load_catalog() -> dict:
    """Load the example catalog JSON."""
    if not CATALOG_PATH.exists():
        logger.error("Example catalog not found at %s", CATALOG_PATH)
        return {"categories": {}}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def get_catalog_prompt() -> str:
    """Generate a compact catalog summary for the codegen system prompt.

    Returns a text block listing each category name, description, and example
    summaries — roughly 2-3K tokens — so the model can choose categories.
    """
    catalog = _load_catalog()
    lines = ["# Available Manim Example Categories", ""]
    lines.append(
        "Below are categories of real Manim code examples. "
        "You can request any of these categories to see actual working code."
    )
    lines.append("")

    for cat_name, cat_data in catalog.get("categories", {}).items():
        lines.append(f"## {cat_name}")
        lines.append(f"**Description:** {cat_data['description']}")
        lines.append("**Examples:**")
        for ex in cat_data.get("examples", []):
            classes_str = ", ".join(ex.get("classes", []))
            lines.append(f"  - `{ex['id']}` ({classes_str}): {ex['summary']}")
        lines.append("")

    return "\n".join(lines)


def _read_file_lines(filepath: Path, line_spec: str) -> str:
    """Read specific lines from a file.

    Args:
        filepath: Path to the .py file.
        line_spec: Line range like "156-225" or "1-80".

    Returns:
        The content of those lines, or the full file if line_spec is invalid.
    """
    if not filepath.exists():
        logger.warning("Example file not found: %s", filepath)
        return f"# File not found: {filepath}"

    full_text = filepath.read_text(encoding="utf-8", errors="replace")
    all_lines = full_text.splitlines()

    match = re.match(r"(\d+)-(\d+)", line_spec)
    if not match:
        return full_text

    start = max(0, int(match.group(1)) - 1)
    end = min(len(all_lines), int(match.group(2)))
    return "\n".join(all_lines[start:end])


def _read_full_file(filepath: Path) -> str:
    """Read the entire file content."""
    if not filepath.exists():
        logger.warning("Example file not found: %s", filepath)
        return f"# File not found: {filepath}"
    return filepath.read_text(encoding="utf-8", errors="replace")


MAX_EXAMPLES_PER_CATEGORY = int(
    __import__("os").environ.get("MAX_EXAMPLES_PER_CATEGORY", "8")
)
MAX_TOTAL_CHARS = int(
    __import__("os").environ.get("MAX_EXAMPLE_CHARS", "300000")
)


def load_examples_for_categories(categories: list[str]) -> str:
    """Load actual Python code for the requested categories.

    Args:
        categories: List of category names like ["equations_and_tex", "graphs_and_plots"].

    Returns:
        Formatted string with all example code from the requested categories.
    """
    catalog = _load_catalog()
    all_categories = catalog.get("categories", {})

    sections = []
    loaded_files: set[str] = set()
    loaded_examples: list[str] = []
    total_chars = 0

    for cat_name in categories:
        cat_data = all_categories.get(cat_name)
        if not cat_data:
            logger.warning("Unknown category requested: %s", cat_name)
            continue

        cat_example_ids = []
        sections.append(f"\n{'='*60}")
        sections.append(f"CATEGORY: {cat_name}")
        sections.append(f"Description: {cat_data['description']}")
        sections.append(f"{'='*60}\n")

        examples = cat_data.get("examples", [])[:MAX_EXAMPLES_PER_CATEGORY]
        for example in examples:
            if total_chars >= MAX_TOTAL_CHARS:
                logger.info(
                    "Golden examples: hit %d char limit, stopping",
                    MAX_TOTAL_CHARS,
                )
                break

            file_rel = example["file"]

            file_key = f"{file_rel}:{example.get('lines', 'all')}"
            if file_key in loaded_files:
                continue
            loaded_files.add(file_key)

            filepath = BASE_DIR / file_rel
            lines_spec = example.get("lines", "")
            classes_str = ", ".join(example.get("classes", []))

            sections.append(f"# --- {example['id']} ---")
            sections.append(f"# File: {file_rel}")
            sections.append(f"# Classes: {classes_str}")
            sections.append(f"# {example['summary']}")
            sections.append("")

            if lines_spec and "-" in lines_spec:
                code = _read_file_lines(filepath, lines_spec)
            else:
                code = _read_full_file(filepath)

            max_chars = 15000
            if len(code) > max_chars:
                code = code[:max_chars] + "\n# ... (truncated for brevity) ..."

            sections.append(code)
            sections.append("")
            cat_example_ids.append(example["id"])
            total_chars += len(code)

        logger.info(
            "Golden examples: loaded %d examples from '%s': %s",
            len(cat_example_ids), cat_name, ", ".join(cat_example_ids),
        )
        loaded_examples.extend(cat_example_ids)

    logger.info(
        "Golden examples: total %d examples loaded across %d categories (%d chars)",
        len(loaded_examples), len(categories), sum(len(s) for s in sections),
    )

    if not sections:
        return "# No example code available for the requested categories."

    return "\n".join(sections)


def get_available_categories() -> list[str]:
    """Return a list of all available category names."""
    catalog = _load_catalog()
    return list(catalog.get("categories", {}).keys())
