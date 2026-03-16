"""Dynamic prompt assembly system.

Inspired by template-prompt-to-motion-graphics-saas skills architecture.
Assembles codegen, orchestrator, and reviewer prompts from modular pieces:
  - Plugins (JSON): library-specific API docs and patterns
  - Components (TSX): auto-discovered from @component metadata
  - Styles (YAML): visual style presets
  - Examples (TSX): reference scene implementations
  - Patterns (JSON): animation technique catalog and prompt writing tips
  - Format presets: video dimensions and duration constraints

Topic-based skill detection selects relevant plugins and examples.
"""

import json
import re
from pathlib import Path
from typing import Optional

import yaml

_BASE_DIR = Path(__file__).resolve().parent
_PLUGINS_DIR = _BASE_DIR / "plugins"
_STYLES_DIR = _BASE_DIR / "styles"
_EXAMPLES_DIR = _BASE_DIR / "examples"
_PATTERNS_FILE = _BASE_DIR / "prompt_patterns.json"
_COMPONENTS_DIR = _BASE_DIR.parent / "remotion_project" / "src" / "components"
_GOLDEN_DIR = _BASE_DIR / "golden_scenes"
_GOLDEN_INDEX = _GOLDEN_DIR / "index.json"
_PROJECTS_DIR = _BASE_DIR.parent / "remotion-projects-extracted"

# ── Video Format Presets ─────────────────────────────────────────────────

FORMAT_PRESETS = {
    "youtube": {
        "name": "YouTube",
        "width": 1920, "height": 1080, "fps": 30,
        "min_duration_s": 120, "max_duration_s": 900,
        "max_scenes": 18, "pacing": "medium", "content_density": "medium",
    },
    "tiktok": {
        "name": "TikTok",
        "width": 1080, "height": 1920, "fps": 30,
        "min_duration_s": 15, "max_duration_s": 60,
        "max_scenes": 4, "pacing": "fast", "content_density": "high",
    },
    "instagram_reel": {
        "name": "Instagram Reel",
        "width": 1080, "height": 1920, "fps": 30,
        "min_duration_s": 30, "max_duration_s": 90,
        "max_scenes": 6, "pacing": "fast", "content_density": "high",
    },
    "instagram_post": {
        "name": "Instagram Post",
        "width": 1080, "height": 1080, "fps": 30,
        "min_duration_s": 15, "max_duration_s": 60,
        "max_scenes": 3, "pacing": "fast", "content_density": "high",
    },
    "product_demo": {
        "name": "Product Demo",
        "width": 1920, "height": 1080, "fps": 30,
        "min_duration_s": 30, "max_duration_s": 120,
        "max_scenes": 8, "pacing": "medium", "content_density": "high",
    },
    "short_explainer": {
        "name": "Short Explainer",
        "width": 1920, "height": 1080, "fps": 30,
        "min_duration_s": 60, "max_duration_s": 180,
        "max_scenes": 8, "pacing": "medium", "content_density": "high",
    },
}


# ── Caching ──────────────────────────────────────────────────────────────

_cache: dict = {}


def _invalidate_cache():
    _cache.clear()


# ── Plugin Loading ───────────────────────────────────────────────────────

def load_all_plugins() -> dict[str, dict]:
    if "plugins" in _cache:
        return _cache["plugins"]
    plugins = {}
    if _PLUGINS_DIR.exists():
        for f in sorted(_PLUGINS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                plugins[data.get("name", f.stem)] = data
            except Exception as e:
                print(f"[ASSEMBLER] Failed to load plugin {f.name}: {e}")
    _cache["plugins"] = plugins
    return plugins


def detect_relevant_plugins(topic: str, plan_json: Optional[dict] = None) -> list[str]:
    """Select plugins relevant to the topic using keyword matching.

    Uses whole-word matching to avoid false positives (e.g. 'animation' in
    'math visualization library' shouldn't trigger animejs).
    """
    topic_lower = topic.lower()
    plan_text = json.dumps(plan_json).lower() if plan_json else ""
    combined = topic_lower + " " + plan_text
    words = set(re.findall(r'\b\w+\b', combined))

    stemmed_words = words | {w.rstrip("s") for w in words} | {w + "s" for w in words}

    plugins = load_all_plugins()
    selected = []
    for name, plugin in plugins.items():
        capabilities = plugin.get("capabilities", [])
        stemmed_caps = set()
        for cap in capabilities:
            stemmed_caps.add(cap)
            stemmed_caps.add(cap.rstrip("s"))
            stemmed_caps.add(cap + "s")
        if stemmed_caps & stemmed_words:
            selected.append(name)
            continue
        multi_word_caps = [c for c in capabilities if " " in c or "-" in c]
        if any(c in combined for c in multi_word_caps):
            selected.append(name)
    return selected


def get_plugin_prompt_section(plugin_names: list[str]) -> str:
    """Build prompt section for selected plugins."""
    plugins = load_all_plugins()
    sections = []
    for name in plugin_names:
        plugin = plugins.get(name)
        if not plugin:
            continue
        parts = [f"\n=== {plugin['name'].upper()} LIBRARY ==="]
        if plugin.get("api_reference"):
            parts.append(plugin["api_reference"])
        if plugin.get("remotion_integration_notes"):
            parts.append(f"\nINTEGRATION: {plugin['remotion_integration_notes']}")
        if plugin.get("forbidden_patterns"):
            parts.append("\nFORBIDDEN:")
            for fp in plugin["forbidden_patterns"]:
                parts.append(f"  - {fp}")
        sections.append("\n".join(parts))
    return "\n\n".join(sections) if sections else ""


# ── Component Auto-Discovery ────────────────────────────────────────────

def discover_components() -> list[dict]:
    """Scan component files for @component metadata headers."""
    if "components" in _cache:
        return _cache["components"]
    components = []
    if not _COMPONENTS_DIR.exists():
        return components
    def _extract_tag(content: str, tag: str) -> str:
        m = re.search(rf'\*\s*@{tag}\s+(.+)', content)
        return m.group(1).strip() if m else ""

    for f in sorted(_COMPONENTS_DIR.glob("*.tsx")):
        if f.name in ("index.ts", "index.tsx"):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[ASSEMBLER] Failed to read component {f.name}: {e}")
            continue
        components.append({
            "name": _extract_tag(content, "component") or f.stem,
            "description": _extract_tag(content, "description"),
            "when_to_use": _extract_tag(content, "when_to_use"),
            "layout_type": _extract_tag(content, "layout_type") or f.stem,
            "props": _extract_tag(content, "props"),
            "file": f.name,
        })
    _cache["components"] = components
    return components


def get_component_prompt_section() -> str:
    """Build component API reference from discovered metadata."""
    components = discover_components()
    if not components:
        return ""
    lines = ["AVAILABLE PRE-BUILT COMPONENTS (import from '../components'):\n"]
    for i, comp in enumerate(components, 1):
        desc = f" — {comp['description']}" if comp['description'] else ""
        lines.append(f"{i}. {comp['name']}{desc}")
        if comp['props']:
            lines.append(f"   Props: {{{comp['props']}}}")
        if comp['when_to_use']:
            lines.append(f"   Use for: {comp['when_to_use']}")
    return "\n".join(lines)


# ── Style Presets ────────────────────────────────────────────────────────

def load_all_styles() -> dict[str, dict]:
    if "styles" in _cache:
        return _cache["styles"]
    styles = {}
    if _STYLES_DIR.exists():
        for f in sorted(_STYLES_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                styles[data.get("name", f.stem)] = data
            except Exception as e:
                print(f"[ASSEMBLER] Failed to load style {f.name}: {e}")
    _cache["styles"] = styles
    return styles


def get_style_prompt_section(style_name: Optional[str] = None) -> str:
    """Build style guidance for the selected style."""
    styles = load_all_styles()
    if style_name and style_name not in styles:
        print(f"[ASSEMBLER] Unknown style '{style_name}', listing available styles")
    if style_name and style_name in styles:
        style = styles[style_name]
        lines = [f"\n=== VISUAL STYLE: {style.get('display_name', style_name).upper()} ==="]
        lines.append(style.get("description", ""))
        palette = style.get("palette", {})
        if palette:
            lines.append("\nCOLOR PALETTE:")
            for k, v in palette.items():
                lines.append(f"  {k}: {v}")
        typo = style.get("typography", {})
        if typo:
            lines.append("\nTYPOGRAPHY:")
            for k, v in typo.items():
                lines.append(f"  {k}: {v}")
        guidelines = style.get("animation_guidelines", [])
        if guidelines:
            lines.append("\nANIMATION GUIDELINES:")
            for g in guidelines:
                lines.append(f"  - {g}")
        prefs = style.get("layout_preferences", [])
        if prefs:
            lines.append(f"\nPREFERRED LAYOUTS: {', '.join(prefs)}")
        special = style.get("special_instructions", "")
        if special:
            lines.append(f"\nSPECIAL INSTRUCTIONS:\n{special}")
        return "\n".join(lines)

    lines = ["\nAVAILABLE VIDEO STYLES (select one based on topic):"]
    for name, style in styles.items():
        lines.append(f"  - {name}: {style.get('description', '')}")
    return "\n".join(lines)


# ── Example Scenes ───────────────────────────────────────────────────────

def load_all_examples() -> list[dict]:
    if "examples" in _cache:
        return _cache["examples"]
    examples = []
    if _EXAMPLES_DIR.exists():
        for f in sorted(_EXAMPLES_DIR.glob("*.tsx")):
            content = f.read_text(encoding="utf-8")
            meta = {}
            for tag in ("example", "source", "description", "tags", "libraries"):
                m = re.search(rf'@{tag}\s+(.+)', content)
                if m:
                    meta[tag] = m.group(1).strip()
            examples.append({
                "file": f.name,
                "meta": meta,
                "code": content,
            })
    _cache["examples"] = examples
    return examples


def select_relevant_examples(topic: str, max_examples: int = 3) -> list[dict]:
    """Select examples relevant to the topic via tag matching."""
    topic_lower = topic.lower()
    examples = load_all_examples()
    scored = []
    for ex in examples:
        tags = [t.strip() for t in ex["meta"].get("tags", "").split(",")]
        score = sum(1 for tag in tags if tag in topic_lower)
        desc = ex["meta"].get("description", "").lower()
        score += sum(1 for word in topic_lower.split() if word in desc)
        scored.append((score, ex))
    scored.sort(key=lambda x: -x[0])
    return [ex for _, ex in scored[:max_examples]]


def get_examples_prompt_section(examples: list[dict]) -> str:
    """Build prompt section with example scene code."""
    if not examples:
        return ""
    lines = ["\n=== EXAMPLE SCENES (reference implementations — adapt, don't copy) ===\n"]
    for ex in examples:
        name = ex["meta"].get("example", ex["file"])
        desc = ex["meta"].get("description", "")
        lines.append(f"--- Example: {name} ---")
        lines.append(f"Description: {desc}")
        code = ex["code"]
        if len(code) > 3000:
            code = code[:3000] + "\n// ... (truncated)"
        lines.append(f"```tsx\n{code}\n```\n")
    return "\n".join(lines)


# ── Golden Scenes (real-world production examples with thinking) ─────────

_SKIP_FILENAMES = {
    "index.ts", "Root.tsx", "Composition.tsx", "remotion.config.ts",
}


def load_golden_index() -> list[dict]:
    """Load the golden scenes index from index.json."""
    if "golden_index" in _cache:
        return _cache["golden_index"]
    if _GOLDEN_INDEX.exists():
        try:
            data = json.loads(_GOLDEN_INDEX.read_text(encoding="utf-8"))
            _cache["golden_index"] = data
            return data
        except json.JSONDecodeError as e:
            print(f"[ASSEMBLER] Failed to parse golden index: {e}")
    return []


def _resolve_project_dir(entry: dict) -> Path:
    """Resolve the filesystem path for a golden scene project."""
    project = entry["project"]
    sub = entry.get("sub_path", "")
    base = _PROJECTS_DIR / project
    if sub:
        base = base / sub
    return base


def _read_file(path: Path) -> str:
    """Read a text file, returning empty string on failure."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _collect_project_tsx_files(project_dir: Path) -> list[tuple[str, str]]:
    """Collect all meaningful .tsx/.ts files from a project (excluding boilerplate).

    Returns list of (relative_path, content) tuples.
    """
    src_dir = project_dir / "src"
    if not src_dir.exists():
        return []

    results = []
    for fpath in sorted(src_dir.rglob("*.tsx")) + sorted(src_dir.rglob("*.ts")):
        if fpath.name.startswith("._"):
            continue
        if fpath.name in _SKIP_FILENAMES:
            continue
        content = _read_file(fpath)
        if content and len(content) > 50:
            rel = fpath.relative_to(project_dir).as_posix()
            results.append((rel, content))
    return results


def get_golden_scenes_prompt_section(
    topic: str = "",
    layout_type: str = "",
) -> str:
    """Build a prompt section with ALL golden scene projects.

    Gemini 3 Flash has a 1M token context window, so we load everything:
    ~125K tokens covering 10 real-world Remotion projects with:
    - Ideal structured prompts (how scenes should be specified)
    - Step-by-step thinking (how to reason about animation choreography)
    - Complete production code (heavily commented, battle-tested)

    This gives the codegen model a comprehensive reference library of
    professional Remotion patterns, animation techniques, and code quality.
    """
    index = load_golden_index()
    if not index:
        return ""

    if "golden_prompt" in _cache:
        return _cache["golden_prompt"]

    seen_projects: set[str] = set()
    lines = [
        "\n=== GOLDEN REFERENCE LIBRARY (10 production Remotion projects) ===",
        "Below are complete, professionally crafted Remotion projects.",
        "Each includes the original prompt, step-by-step design thinking,",
        "and heavily commented production code. Study these to understand:",
        "  - How to structure animation timelines with precise frame numbers",
        "  - How to choose spring configs (damping, stiffness, mass) for different effects",
        "  - How to layer animations (background, content, accents) for depth",
        "  - How to comment code to explain animation decisions",
        "  - How to build data-driven visualizations (charts, timelines, grids)",
        "  - How to create smooth entrance/exit choreography with staggered delays",
        "Your output should match this quality level.\n",
    ]

    for entry in index:
        project_key = entry["project"] + entry.get("sub_path", "")
        if project_key in seen_projects:
            continue
        seen_projects.add(project_key)

        project_dir = _resolve_project_dir(entry)
        if not project_dir.exists():
            continue

        project_name = entry["project"]
        lines.append(f"{'=' * 70}")
        lines.append(f"PROJECT: {project_name}")
        lines.append(f"{'=' * 70}")

        ideal_prompt = _read_file(project_dir / "prompt_claude.txt")
        if ideal_prompt:
            lines.append(f"\n### IDEAL PROMPT (how this project should be specified):\n")
            lines.append(f"```\n{ideal_prompt}\n```")

        thinking = _read_file(project_dir / "thinking.txt")
        if thinking:
            lines.append(f"\n### DESIGN THINKING (step-by-step reasoning process):\n")
            lines.append(f"```\n{thinking}\n```")

        tsx_files = _collect_project_tsx_files(project_dir)
        if tsx_files:
            lines.append(f"\n### PRODUCTION CODE ({len(tsx_files)} file(s)):\n")
            for rel_path, code in tsx_files:
                lines.append(f"#### {rel_path}")
                lines.append(f"```tsx\n{code}\n```\n")

        lines.append("")

    result = "\n".join(lines)
    _cache["golden_prompt"] = result
    total_chars = len(result)
    est_tokens = total_chars // 4
    print(f"[ASSEMBLER] Golden reference library: {total_chars:,} chars (~{est_tokens:,} tokens, {len(seen_projects)} projects)")
    return result


# ── Animation Patterns ───────────────────────────────────────────────────

def load_patterns() -> dict:
    if "patterns" in _cache:
        return _cache["patterns"]
    if _PATTERNS_FILE.exists():
        try:
            data = json.loads(_PATTERNS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"[ASSEMBLER] Failed to parse prompt_patterns.json: {e}")
            return {}
        _cache["patterns"] = data
        return data
    return {}


def get_patterns_prompt_section(style_name: Optional[str] = None) -> str:
    """Build animation technique catalog for the codegen prompt."""
    patterns = load_patterns()
    if not patterns:
        return ""
    lines = ["\n=== ANIMATION TECHNIQUE CATALOG ===\n"]

    springs = patterns.get("spring_configs", {})
    if springs:
        lines.append("SPRING CONFIGURATIONS (use these named presets):")
        for name, cfg in springs.items():
            lines.append(f"  {name}: damping={cfg['damping']}, stiffness={cfg.get('stiffness','')}, mass={cfg.get('mass','')} — {cfg['description']}")
        lines.append("")

    techniques = patterns.get("animation_techniques", [])
    if techniques:
        lines.append("ANIMATION TECHNIQUES:")
        for tech in techniques:
            lines.append(f"  {tech['name']}: {tech['description']}")
            lines.append(f"    Pattern: {tech['code_pattern'][:200]}")
        lines.append("")

    timing = patterns.get("timing_patterns", {})
    if timing:
        lines.append("TIMING REFERENCE (frame counts at 30fps):")
        for k, v in timing.items():
            lines.append(f"  {k}: {v}")
        lines.append("")

    tips = patterns.get("prompt_writing_patterns", {})
    if tips:
        lines.append("PROMPT QUALITY TIPS:")
        for k, v in tips.items():
            lines.append(f"  {v}")

    return "\n".join(lines)


# ── Format Preset ────────────────────────────────────────────────────────

def get_format_preset(format_name: str = "youtube") -> dict:
    if format_name not in FORMAT_PRESETS:
        print(f"[ASSEMBLER] Unknown format '{format_name}', defaulting to youtube")
    return FORMAT_PRESETS.get(format_name, FORMAT_PRESETS["youtube"])


def get_format_prompt_section(format_name: str = "youtube") -> str:
    """Build format-specific instructions for codegen."""
    preset = get_format_preset(format_name)
    lines = [f"\n=== VIDEO FORMAT: {preset['name']} ==="]
    lines.append(f"Resolution: {preset['width']}x{preset['height']} @ {preset['fps']}fps")
    lines.append(f"Duration: {preset['min_duration_s']}-{preset['max_duration_s']} seconds")
    lines.append(f"Max scenes: {preset['max_scenes']}")
    lines.append(f"Pacing: {preset['pacing']}")
    lines.append(f"Content density: {preset['content_density']}")
    if preset["max_duration_s"] <= 120:
        lines.append("\nSHORT VIDEO RULES:")
        lines.append("  - Jump straight into content — no lengthy intro or conclusion scenes")
        lines.append("  - Higher information density per frame")
        lines.append("  - Faster transitions (halve fade durations)")
        lines.append("  - Each scene must deliver a complete thought quickly")
        lines.append("  - Prefer fast pacing spring configs (elastic_pop, snap)")
    if preset["width"] < preset["height"]:
        lines.append("\nVERTICAL FORMAT RULES:")
        lines.append("  - Stack content vertically, not side-by-side")
        lines.append("  - Use full-width text blocks")
        lines.append("  - Avoid SplitScreen layout (doesn't work in portrait)")
        lines.append("  - Center-align content")
        lines.append("  - Larger text sizes (readable on mobile)")
    return "\n".join(lines)


# ── Orchestrator Prompt ──────────────────────────────────────────────────

def get_orchestrator_format_section(format_name: str = "youtube") -> str:
    """Build format-specific orchestrator instructions."""
    preset = get_format_preset(format_name)
    min_s, max_s = preset["min_duration_s"], preset["max_duration_s"]
    max_scenes = preset["max_scenes"]
    lines = [
        f"\nVIDEO FORMAT: {preset['name']} ({preset['width']}x{preset['height']})",
        f"Target duration: {min_s}-{max_s} seconds ({min_s // 60}-{max_s // 60} minutes)" if max_s >= 120
        else f"Target duration: {min_s}-{max_s} seconds",
        f"Plan exactly {max_scenes} scenes maximum.",
        f"Pacing: {preset['pacing']} | Content density: {preset['content_density']}",
    ]
    if max_s <= 120:
        lines.extend([
            "",
            "SHORT VIDEO PLANNING RULES:",
            "  - Fewer scenes, each with DENSER content",
            "  - No separate 'introduction' or 'conclusion' scenes — embed hooks and CTAs inline",
            "  - Higher information density per frame",
            "  - Faster transitions (fade duration halved)",
            "  - Skip narration pauses between sections",
            "  - Each narration: 2-3 punchy sentences, 30-40 words max per scene",
        ])
    return "\n".join(lines)


# ── Full Prompt Assembly ─────────────────────────────────────────────────

def assemble_codegen_system_prompt(
    topic: str,
    plan_json: Optional[dict] = None,
    style_name: Optional[str] = None,
    format_name: str = "youtube",
    base_prompt: str = "",
    component_api: str = "",
    remotion_api: str = "",
    anti_slideshow: str = "",
    review_prompt: str = "",
    layout_type: str = "",
) -> str:
    """Assemble the full codegen system prompt.

    Combines the base prompt sections with dynamically selected:
    - Plugin-specific API docs (based on topic/plan keywords)
    - Style-specific guidelines
    - Relevant example scenes
    - Golden scene examples (production-quality code with thinking)
    - Animation technique catalog
    - Format-specific constraints
    """
    relevant_plugins = detect_relevant_plugins(topic, plan_json)
    plugin_section = get_plugin_prompt_section(relevant_plugins)

    relevant_examples = select_relevant_examples(topic)
    examples_section = get_examples_prompt_section(relevant_examples)

    golden_section = get_golden_scenes_prompt_section(topic, layout_type)

    style_section = get_style_prompt_section(style_name)
    patterns_section = get_patterns_prompt_section(style_name)
    format_section = get_format_prompt_section(format_name)

    sections = [base_prompt]

    if component_api:
        sections.append(component_api)

    if remotion_api:
        sections.append(remotion_api)

    if format_section:
        sections.append(format_section)

    if plugin_section:
        sections.append("\n## TOPIC-SPECIFIC LIBRARY GUIDANCE\n" + plugin_section)

    if style_section:
        sections.append(style_section)

    if patterns_section:
        sections.append(patterns_section)

    if examples_section:
        sections.append(examples_section)

    if golden_section:
        sections.append(golden_section)

    if anti_slideshow:
        sections.append(anti_slideshow)

    assembled = "\n\n".join(s for s in sections if s.strip())
    est_tokens = len(assembled) // 4
    print(f"[ASSEMBLER] Codegen prompt: {len(assembled):,} chars (~{est_tokens:,} tokens)")
    if est_tokens > 200000:
        print(f"[ASSEMBLER] Warning: prompt exceeds 200K tokens — may impact response quality")
    return assembled


def assemble_orchestrator_instruction(
    base_instruction: str,
    style_name: Optional[str] = None,
    format_name: str = "youtube",
) -> str:
    """Assemble orchestrator instruction with style and format context."""
    sections = [base_instruction]

    format_section = get_orchestrator_format_section(format_name)
    if format_section:
        sections.append(format_section)

    style_section = get_style_prompt_section(style_name)
    if style_section:
        sections.append(style_section)

    available_styles = get_style_prompt_section(None)
    if available_styles and not style_name:
        sections.append(available_styles)

    return "\n\n".join(s for s in sections if s.strip())


def assemble_reviewer_prompt(
    base_review_prompt: str,
    style_name: Optional[str] = None,
) -> str:
    """Add style-specific review criteria to the reviewer prompt."""
    sections = [base_review_prompt]
    if style_name:
        styles = load_all_styles()
        style = styles.get(style_name, {})
        if style:
            sections.append(
                f"\nSTYLE CONSISTENCY CHECK — {style.get('display_name', style_name)}:\n"
                f"Verify the scene matches the {style_name} style:\n"
                f"  - Color palette: uses {style_name} colors (see palette above)\n"
                f"  - Animation feel: {style.get('pacing', 'medium')} pacing\n"
            )
            guidelines = style.get("animation_guidelines", [])
            if guidelines:
                sections.append("  Style-specific checks:")
                for g in guidelines:
                    sections.append(f"    - {g}")
    return "\n".join(sections)
