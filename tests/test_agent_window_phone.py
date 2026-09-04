from pathlib import Path
from html.parser import HTMLParser
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]


class _ElementTreeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = {"tag": "root", "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"img", "input", "link", "meta", "br", "hr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return


def _find_by_id(node, element_id):
    if node["attrs"].get("id") == element_id:
        return node
    for child in node["children"]:
        found = _find_by_id(child, element_id)
        if found is not None:
            return found
    return None


def _css_rule(style, selector):
    return style.split(f"{selector} {{", 1)[1].split("}", 1)[0]


def test_navigator_project_cards_keep_their_content_height():
    style = (ROOT / "bridge_phone" / "static" / "style.css").read_text(
        encoding="utf-8"
    )

    project_rule = style.split(".navigator-project {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto" in project_rule


def test_agent_workspace_places_composer_after_conversation():
    parser = _ElementTreeParser()
    parser.feed((ROOT / "bridge_phone" / "index.html").read_text(encoding="utf-8"))

    agent_window = _find_by_id(parser.root, "agent-window")
    child_ids = [child["attrs"].get("id") for child in agent_window["children"]]

    assert child_ids[-1] == "agent-composer-slot"


def test_navigator_uses_the_phone_pages_natural_scroll():
    style = (ROOT / "bridge_phone" / "static" / "style.css").read_text(
        encoding="utf-8"
    )

    navigator_rule = _css_rule(style, ".agent-navigator")
    sidebar_rule = _css_rule(style, "#agent-sidebar-card")

    assert "max-height" not in navigator_rule
    assert "overflow-y: auto" not in navigator_rule
    assert "position: sticky" not in sidebar_rule


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
