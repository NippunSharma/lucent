"""Process teacher-provided inputs into a structured context object.

Handles PDFs, handwritten notes (images), URLs, and topic-based research.
Uses Gemini Vision for OCR and content extraction, Google Search for research.
"""

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx
from google import genai
from google.genai.types import (
    Content,
    GenerateContentConfig,
    GoogleSearch,
    Part,
    ThinkingConfig,
    Tool,
)

from .cache import cache_research, get_cached_research

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-devpost-hackathon")


@dataclass
class ReferenceMaterial:
    source: str  # e.g. "pdf:page_3", "notes:image_1", "web:url"
    content: str
    source_type: str  # "pdf", "handwritten", "image", "web"
    page_or_section: str  # "Page 3", "Section 2"


@dataclass
class Context:
    topic: str
    research_brief: dict = field(default_factory=dict)
    references: list[ReferenceMaterial] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "research_brief": self.research_brief,
            "references": [asdict(r) for r in self.references],
        }

    def to_prompt_string(self) -> str:
        """Serialize context for inclusion in LLM prompts."""
        parts = [f"## Topic\n{self.topic}\n"]

        if self.research_brief:
            parts.append("## Research Brief")
            parts.append(json.dumps(self.research_brief, indent=2))
            parts.append("")

        if self.references:
            parts.append("## Reference Materials")
            for i, ref in enumerate(self.references, 1):
                parts.append(
                    f"### Reference {i} [{ref.source_type}] — {ref.page_or_section}"
                )
                parts.append(f"Source: {ref.source}")
                parts.append(ref.content)
                parts.append("")

        return "\n".join(parts)


RESEARCH_PROMPT = r"""You are a research assistant preparing material for an animated educational video made with ManimGL (3Blue1Brown style).
Given a topic, produce a thorough research brief.

OUTPUT FORMAT (strict JSON):
{
  "topic_title": "A clear, engaging title for this topic",
  "summary": "2-3 sentence executive summary of the topic",
  "subtopics": [
    {
      "title": "Subtopic name",
      "key_points": ["fact 1", "fact 2", "fact 3"],
      "statistics": ["stat with source if available"],
      "manim_visual_idea": "How to visualize this with ManimGL (equations, graphs, 3D, geometric constructions)",
      "depth": "brief | moderate | detailed"
    }
  ],
  "hook": "An opening fact, question, or statement to grab attention",
  "common_misconceptions": ["misconception 1", "misconception 2"],
  "real_world_applications": ["application 1", "application 2"],
  "memorable_takeaway": "A closing thought that stays with the viewer",
  "suggested_narrative_arc": "Brief description of how to structure the story",
  "key_equations": ["equation_1_latex", "equation_2_latex"],
  "visual_progression": "How visuals should build from simple to complex"
}

REQUIREMENTS:
- Include subtopics covering the topic comprehensively
- Each subtopic should have 3-5 key points with verified facts
- Include at least 5 statistics or specific numbers
- Suggest ManimGL visualization ideas (equations, graphs, 3D surfaces, geometric proofs)
- The hook should be surprising or thought-provoking
- Include key equations in LaTeX format where relevant
- Use web search results to verify facts and find recent information

Output ONLY valid JSON. No markdown fences, no extra text."""

PDF_EXTRACTION_PROMPT = """Analyze this PDF page and extract ALL content relevant for an educational video:

1. All text content (preserve structure)
2. All mathematical equations (convert to LaTeX)
3. Descriptions of any diagrams, figures, or illustrations
4. Key concepts and definitions
5. Any worked examples or proofs

Format your response as:
## Text Content
[extracted text]

## Equations
[LaTeX equations, one per line]

## Figures/Diagrams
[descriptions of visual elements]

## Key Concepts
[bullet points of main ideas]"""

NOTES_OCR_PROMPT = """Analyze these handwritten notes and extract ALL content:

1. All handwritten text (even if partially illegible, make your best guess)
2. All mathematical equations and expressions (convert to LaTeX)
3. Descriptions of any diagrams, arrows, or visual elements
4. The logical flow and organization of the notes

Format your response as:
## Transcribed Text
[transcribed content preserving structure]

## Equations
[LaTeX equations, one per line]

## Diagrams/Visuals
[descriptions of drawn elements]

## Key Points
[main ideas from the notes]"""


def _get_client() -> genai.Client:
    return genai.Client(vertexai=True, project=PROJECT_ID, location="global")


def _research_topic(topic: str) -> dict:
    """Web-grounded research using Gemini + Google Search."""
    cached = get_cached_research(topic)
    if cached:
        logger.info("Research: cache hit for '%s'", topic)
        return cached

    logger.info("Research: generating brief for '%s'", topic)
    t0 = time.time()
    client = _get_client()

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
        logger.warning("Research: grounded search failed (%s), falling back", e)
        try:
            response = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=f"{RESEARCH_PROMPT}\n\nTOPIC: {topic}",
                config=GenerateContentConfig(
                    thinking_config=ThinkingConfig(thinking_budget=8000),
                ),
            )
        except Exception as e2:
            logger.error("Research: fallback also failed: %s", e2)
            return {"topic_title": topic, "summary": "", "subtopics": []}

    raw_text = ""
    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.text:
                raw_text += part.text

    clean = raw_text.strip()
    if clean.startswith("```"):
        fence = re.search(r"```(?:json)?\s*\n(.*?)```", clean, re.S)
        if fence:
            clean = fence.group(1).strip()

    try:
        brief = json.loads(clean)
    except json.JSONDecodeError:
        brief = {"raw_research": raw_text, "topic_title": topic}

    elapsed = time.time() - t0
    logger.info("Research: brief generated in %.1fs", elapsed)
    cache_research(topic, brief)
    return brief


def _process_pdf(pdf_path: Path) -> list[ReferenceMaterial]:
    """Extract content from a PDF using Gemini Vision."""
    logger.info("Processing PDF: %s", pdf_path.name)
    client = _get_client()

    pdf_bytes = pdf_path.read_bytes()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[
                Content(
                    parts=[
                        Part(
                            inline_data={"mime_type": "application/pdf", "data": pdf_b64}
                        ),
                        Part(text=PDF_EXTRACTION_PROMPT),
                    ]
                )
            ],
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=4000),
            ),
        )
    except Exception as e:
        logger.error("PDF processing failed for %s: %s", pdf_path.name, e)
        return [
            ReferenceMaterial(
                source=f"pdf:{pdf_path.name}",
                content=f"[PDF processing failed: {e}]",
                source_type="pdf",
                page_or_section="All",
            )
        ]

    text = ""
    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.text:
                text += part.text

    return [
        ReferenceMaterial(
            source=f"pdf:{pdf_path.name}",
            content=text,
            source_type="pdf",
            page_or_section="All pages",
        )
    ]


def _process_handwritten_notes(image_path: Path) -> list[ReferenceMaterial]:
    """OCR handwritten notes using Gemini Vision."""
    logger.info("Processing handwritten notes: %s", image_path.name)
    client = _get_client()

    img_bytes = image_path.read_bytes()
    suffix = image_path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif"}
    mime_type = mime_map.get(suffix, "image/jpeg")
    img_b64 = base64.b64encode(img_bytes).decode("utf-8")

    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[
                Content(
                    parts=[
                        Part(inline_data={"mime_type": mime_type, "data": img_b64}),
                        Part(text=NOTES_OCR_PROMPT),
                    ]
                )
            ],
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=4000),
            ),
        )
    except Exception as e:
        logger.error("Notes OCR failed for %s: %s", image_path.name, e)
        return [
            ReferenceMaterial(
                source=f"notes:{image_path.name}",
                content=f"[OCR failed: {e}]",
                source_type="handwritten",
                page_or_section="Image",
            )
        ]

    text = ""
    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.text:
                text += part.text

    return [
        ReferenceMaterial(
            source=f"notes:{image_path.name}",
            content=text,
            source_type="handwritten",
            page_or_section="Image",
        )
    ]


def _process_url(url: str) -> ReferenceMaterial:
    """Fetch and extract key content from a URL."""
    logger.info("Processing URL: %s", url)

    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        raw_html = resp.text[:50000]  # cap at 50K chars
    except Exception as e:
        logger.error("URL fetch failed for %s: %s", url, e)
        return ReferenceMaterial(
            source=f"web:{url}",
            content=f"[Failed to fetch: {e}]",
            source_type="web",
            page_or_section="URL",
        )

    client = _get_client()
    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=(
                "Extract the key educational content from this webpage. "
                "Include all important facts, equations (in LaTeX), definitions, "
                "and key concepts. Ignore navigation, ads, and boilerplate.\n\n"
                f"URL: {url}\n\nPAGE CONTENT:\n{raw_html}"
            ),
            config=GenerateContentConfig(
                thinking_config=ThinkingConfig(thinking_budget=4000),
            ),
        )
        text = ""
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text += part.text
    except Exception as e:
        logger.error("URL content extraction failed for %s: %s", url, e)
        text = raw_html[:5000]

    return ReferenceMaterial(
        source=f"web:{url}",
        content=text,
        source_type="web",
        page_or_section="URL",
    )


def process_context(
    topic: str,
    files: list[Path] | None = None,
    urls: list[str] | None = None,
) -> Context:
    """Process all teacher inputs into a structured context.

    Args:
        topic: The educational topic or prompt.
        files: Optional list of uploaded file paths (PDFs and images).
        urls: Optional list of reference URLs.

    Returns:
        A Context object with research brief and all extracted references.
    """
    ctx = Context(topic=topic)

    ctx.research_brief = _research_topic(topic)

    references: list[ReferenceMaterial] = []

    if files:
        for fpath in files:
            fpath = Path(fpath)
            if not fpath.exists():
                logger.warning("File not found, skipping: %s", fpath)
                continue

            suffix = fpath.suffix.lower()
            if suffix == ".pdf":
                references.extend(_process_pdf(fpath))
            elif suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                references.extend(_process_handwritten_notes(fpath))
            else:
                logger.warning("Unsupported file type, skipping: %s", fpath)

    if urls:
        for url in urls:
            url = url.strip()
            if url:
                references.append(_process_url(url))

    ctx.references = references
    logger.info(
        "Context processed: topic='%s', %d references",
        topic, len(references),
    )
    return ctx
