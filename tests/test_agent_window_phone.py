from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]


def test_navigator_project_cards_keep_their_content_height():
    style = (ROOT / "bridge_phone" / "static" / "style.css").read_text(
        encoding="utf-8"
    )

    project_rule = style.split(".navigator-project {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto" in project_rule


def test_worktree_indicator_uses_a_real_svg_icon():
    html = (ROOT / "bridge_phone" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "bridge_phone" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    svg_path = ROOT / "bridge_phone" / "static" / "worktree.svg"

    assert "/static/worktree.svg" in html
    assert "/static/worktree.svg" in script
    assert "↗" not in html
    assert 'worktree.textContent = "↗"' not in script

    root = ElementTree.parse(svg_path).getroot()
    assert root.tag.endswith("svg")
    assert root.attrib["viewBox"] == "0 0 16 16"


def test_project_chevron_uses_a_centered_svg_icon():
    script = (ROOT / "bridge_phone" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style = (ROOT / "bridge_phone" / "static" / "style.css").read_text(
        encoding="utf-8"
    )
    svg_path = ROOT / "bridge_phone" / "static" / "project-chevron.svg"

    assert "/static/project-chevron.svg" in script
    assert (
        'chevron.className = "navigator-chevron";\n'
        '    chevron.textContent = "▾"'
    ) not in script

    chevron_rule = style.split(".navigator-chevron {", 1)[1].split("}", 1)[0]
    assert "width: 16px" in chevron_rule
    assert "height: 16px" in chevron_rule
    assert "justify-self: center" in chevron_rule

    root = ElementTree.parse(svg_path).getroot()
    assert root.tag.endswith("svg")
    assert root.attrib["viewBox"] == "0 0 16 16"
