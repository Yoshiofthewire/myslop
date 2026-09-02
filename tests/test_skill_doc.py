import re
from pathlib import Path

# Named myslop-handoff, not handoff: a plain "handoff" collides with other skills already
# installed under ~/.claude/skills/. The directory name and the frontmatter name must agree,
# because the install is a straight copy of this directory.
SKILL = Path(__file__).resolve().parents[1] / "skills" / "myslop-handoff" / "SKILL.md"


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_has_frontmatter_with_name_and_description():
    text = SKILL.read_text()
    assert text.startswith("---\n")
    front = text.split("---", 2)[1]
    assert re.search(r"^name:\s*myslop-handoff\s*$", front, re.M)
    assert re.search(r"^description:\s*\S", front, re.M)


def test_every_documented_endpoint_exists_in_the_app(db_path):
    from handoff.app import create_app

    app = create_app(db_path)
    # Read served routes from the OpenAPI schema rather than app.routes: FastAPI wraps
    # included routers in a lazy _IncludedRouter, so app.routes doesn't flatten to
    # concrete Route objects until something forces route resolution. The schema is
    # what actually gets served, so it's the ground truth either way.
    schema = app.openapi()
    served = {(m.upper(), path) for path, methods in schema["paths"].items() for m in methods}

    documented = set(re.findall(r"\b(GET|POST)\s+(/api/[a-z0-9{}/_-]+)", SKILL.read_text()))
    assert documented, "skill documents no endpoints"

    normalised = {
        (m, re.sub(r"\{[a-z_]+\}", "{slug}", p.split("?")[0].rstrip("/"))) for m, p in documented
    }
    served_norm = {(m, re.sub(r"\{[a-z_]+\}", "{slug}", p.rstrip("/"))) for m, p in served}

    assert normalised <= served_norm, f"undocumented-or-wrong: {normalised - served_norm}"


def test_skill_states_the_expiry_rule():
    text = SKILL.read_text().lower()
    assert "7 day" in text or "seven day" in text
    # \s+ tolerates the doc's own line wrap between "not" and "memory".
    assert re.search(r"not\s+memory", text)


def test_skill_requires_a_named_author_note():
    text = SKILL.read_text()
    # The three-part note is the only thing distinguishing two instances sharing a token,
    # so the format and the pick-once rule both have to survive edits to this document.
    assert "<system> / <model> / <your own name>" in text
    assert re.search(r"Choose your own name once", text)
