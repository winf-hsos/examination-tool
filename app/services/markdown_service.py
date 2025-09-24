from __future__ import annotations

from typing import Dict, Iterable

import bleach
import markdown


ALLOWED_TAGS: Iterable[str] = bleach.sanitizer.ALLOWED_TAGS.union(
    {
        "p",
        "pre",
        "code",
        "span",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
    }
)

ALLOWED_ATTRIBUTES: Dict[str, Iterable[str]] = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "span": ["class"],
    "div": ["class"],
    "code": ["class"],
    "th": ["align"],
    "td": ["align"],
}


def render_markdown(content: str | None) -> str | None:
    if not content:
        return None

    html = markdown.markdown(
        content,
        extensions=[
            "fenced_code",
            "tables",
            "codehilite",
            "md_in_html",
        ],
        output_format="html5",
    )
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES)
    return cleaned
