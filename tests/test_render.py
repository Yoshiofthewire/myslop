import pytest

from handoff.render import render

# Each payload must not survive rendering. The assertion is on the absence of the
# executable part, not on an exact output string, so a sanitizer upgrade that changes
# formatting does not produce a false failure.
XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '<a href="javascript:alert(1)">click</a>',
    "<svg onload=alert(1)></svg>",
    '<iframe src="data:text/html,<script>alert(1)</script>"></iframe>',
    '<div style="background:url(javascript:alert(1))">x</div>',
    '<object data="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="></object>',
    '<form action="http://evil"><input name=x></form>',
    '<a href="vbscript:alert(1)">x</a>',
    "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
    "<style>@import 'http://evil/x.css';</style>",
    '<base href="http://evil/">',
    '<a href="&#106;avascript:alert(1)">x</a>',
]

FORBIDDEN = [
    "<script",
    "onerror",
    "onload",
    "javascript:",
    "vbscript:",
    "<iframe",
    "<object",
    "<form",
    "<style",
    "<base",
    "srcdoc",
]


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
@pytest.mark.parametrize("fmt", ["md", "html"])
def test_xss_payloads_do_not_survive(payload, fmt):
    out = render(payload, fmt, {}).lower()
    for needle in FORBIDDEN:
        assert needle not in out, f"{needle!r} survived {fmt} rendering of {payload!r}"


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_text_format_escapes_every_payload(payload):
    # `text` is escaped, not sanitized, so the FORBIDDEN substring check does not apply --
    # "onerror" legitimately survives as the literal text "onerror". The property that
    # matters is that no markup survives at all.
    out = render(payload, "text", {})
    assert out.startswith("<pre>") and out.endswith("</pre>")
    inner = out[len("<pre>") : -len("</pre>")]
    assert "<" not in inner
    assert ">" not in inner


def test_markdown_basics_render():
    out = render("# Title\n\n- a\n- b\n", "md", {})
    assert "<h1>Title</h1>" in out
    assert "<li>a</li>" in out


def test_markdown_tables_are_enabled():
    out = render("| a | b |\n| - | - |\n| 1 | 2 |\n", "md", {})
    assert "<table>" in out
    assert "<td>1</td>" in out


def test_text_format_is_escaped_and_preformatted():
    out = render("<b>not bold</b>", "text", {})
    assert out.startswith("<pre>")
    assert "&lt;b&gt;" in out
    assert "<b>" not in out


def test_safe_html_passes_through():
    out = render("<p>hello <strong>there</strong></p>", "html", {})
    assert "<strong>there</strong>" in out


def test_links_get_rel_and_keep_https():
    out = render("[x](https://example.com)", "md", {})
    assert 'href="https://example.com"' in out
    assert "noopener" in out
    assert "nofollow" in out


def test_img_reference_is_resolved_to_blob_url():
    out = render("![arch](img:arch.png)", "md", {"arch.png": "/f/s/blob/abc123"})
    assert 'src="/f/s/blob/abc123"' in out
    assert "img:" not in out


def test_unresolved_img_reference_is_dropped():
    out = render("![missing](img:nope.png)", "md", {})
    assert "img:nope.png" not in out


def test_img_resolution_does_not_apply_to_arbitrary_text():
    out = render(
        "the string img:arch.png is not an image here", "text", {"arch.png": "/f/s/blob/x"}
    )
    assert "/f/s/blob/x" not in out


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        render("x", "pdf", {})
