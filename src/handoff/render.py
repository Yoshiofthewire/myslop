"""Markdown rendering and the single sanitization choke point.

Nothing outside this module may produce a value stored in ``posts.html``.
"""

import html as html_escape
import re

import nh3
from markdown_it import MarkdownIt

ALLOWED_TAGS = {
    "p",
    "br",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "strong",
    "em",
    "del",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "img",
    "details",
    "summary",
}

ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

URL_SCHEMES = {"http", "https", "mailto"}

_md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])

_FORMATS = ("md", "html", "text")


def _resolve_images(source: str, blob_urls: dict[str, str]) -> str:
    """Replace ``img:<filename>`` in URL position with the stored blob URL."""
    if not blob_urls:
        return source
    for filename, url in blob_urls.items():
        source = re.sub(
            r"(?<=[(\"'])img:" + re.escape(filename) + r"(?=[)\"'\s])",
            url,
            source,
        )
    return source


def sanitize(dirty: str) -> str:
    return nh3.clean(
        dirty,
        tags=ALLOWED_TAGS,
        attributes={k: set(v) for k, v in ALLOWED_ATTRS.items()},
        url_schemes=URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def render(source: str, fmt: str, blob_urls: dict[str, str]) -> str:
    """Turn a submitted body into HTML that is safe to embed in a page."""
    if fmt not in _FORMATS:
        raise ValueError(f"unknown format: {fmt!r}")

    if fmt == "text":
        return "<pre>" + html_escape.escape(source) + "</pre>"

    resolved = _resolve_images(source, blob_urls)
    dirty = _md.render(resolved) if fmt == "md" else resolved
    return sanitize(dirty)
