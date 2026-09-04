import numpy as np

import press_codex_accessibility as accessibility


class FakeExpandPattern:
    def __init__(self, expanded=True):
        self.ExpandCollapseState = 1 if expanded else 0
        self.expanded = expanded

    def Expand(self):
        self.expanded = True
        self.ExpandCollapseState = 1

    def Collapse(self):
        self.expanded = False
        self.ExpandCollapseState = 0


class FakeProjectControl:
    def __init__(self, expanded=True):
        self.pattern = FakeExpandPattern(expanded)

    def GetExpandCollapsePattern(self):
        return self.pattern


class FakeInvokePattern:
    def __init__(self):
        self.invoked = False

    def Invoke(self):
        self.invoked = True


class FakeTaskControl:
    def __init__(self):
        self.pattern = FakeInvokePattern()

    def GetInvokePattern(self):
        return self.pattern


def node(kind, name, class_name, rect, control=None):
    return accessibility._Node(kind, name, class_name, rect, control=control)


def sample_nodes():
    project_control = FakeProjectControl(expanded=True)
    done_control = FakeTaskControl()
    busy_control = FakeTaskControl()
    nodes = [
        node(
            "ButtonControl",
            "SummonLab private_3",
            "group/folder-row sidebar-item",
            (0, 0, 300, 40),
            project_control,
        ),
        node(
            "ButtonControl",
            "Start new chat in SummonLab",
            "button",
            (245, 0, 40, 40),
        ),
        node(
            "ImageControl", "Connected", "icon-2xs text-chart-green", (270, 8, 16, 16)
        ),
        node(
            "ButtonControl",
            "Finished task",
            "sidebar-item py-row-y bg-primary-ghost-hover",
            (0, 40, 300, 40),
            done_control,
        ),
        node(
            "ImageControl",
            "",
            "icon-2xs text-codex-description no-drag shrink-0",
            (245, 50, 16, 16),
        ),
        node(
            "ButtonControl",
            "Running task",
            "sidebar-item py-row-y",
            (0, 80, 300, 40),
            busy_control,
        ),
        node("ImageControl", "", "icon-xs shrink-0", (270, 88, 20, 20)),
    ]
    return nodes, project_control, done_control, busy_control


def test_build_navigator_model_extracts_semantics_and_pixel_state():
    nodes, *_ = sample_nodes()
    rgb = np.zeros((140, 300, 3), dtype=np.uint8)
    rgb[52:60, 274:282] = (45, 120, 245)

    model = accessibility.build_navigator_model(nodes, (0, 0, 300, 140), rgb)

    [project] = model["projects"]
    assert project == {
        "name": "SummonLab",
        "host": "private_3",
        "connected": True,
        "state": "connected",
        "expanded": True,
        "tasks": [
            {
                "title": "Finished task",
                "state": "done",
                "worktree": True,
                "selected": True,
            },
            {
                "title": "Running task",
                "state": "busy",
                "worktree": False,
                "selected": False,
            },
        ],
    }


def test_missing_foreground_pixels_does_not_claim_task_is_idle():
    nodes, *_ = sample_nodes()

    model = accessibility.build_navigator_model(nodes, (0, 0, 300, 140))

    tasks = model["projects"][0]["tasks"]
    assert tasks[0]["state"] == "unknown"
    assert tasks[1]["state"] == "busy"


def test_navigator_action_invokes_task_without_mouse(monkeypatch):
    nodes, _project, done, _busy = sample_nodes()
    monkeypatch.setattr(accessibility, "_walk_nodes", lambda _hwnd: nodes)
    window = {
        "backend": "codex_desktop",
        "hwnd": 42,
        "region": [0, 0, 300, 140],
        "agent_surfaces": {"sidebar": [0, 0, 1, 1]},
    }

    result = accessibility.perform_codex_navigator_action(
        window,
        {"kind": "task", "project": "SummonLab", "title": "Finished task"},
    )

    assert result["acted"] is True
    assert done.pattern.invoked is True


def test_navigator_action_sets_project_expansion_idempotently(monkeypatch):
    nodes, project, *_ = sample_nodes()
    monkeypatch.setattr(accessibility, "_walk_nodes", lambda _hwnd: nodes)
    window = {
        "backend": "codex_desktop",
        "hwnd": 42,
        "region": [0, 0, 300, 140],
        "agent_surfaces": {"sidebar": [0, 0, 1, 1]},
    }

    result = accessibility.perform_codex_navigator_action(
        window,
        {"kind": "project", "project": "SummonLab", "expanded": False},
    )

    assert result["expanded"] is False
    assert project.pattern.expanded is False
