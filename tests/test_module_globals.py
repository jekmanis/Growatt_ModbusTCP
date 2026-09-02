"""Every global name a runtime module uses has to be bound somewhere in that module.

This is the one class of defect that a `try: ... except Exception:` turns invisible.
`__init__.async_setup_entry` called `ir.async_create_issue(...)` and
`ir.IssueSeverity.ERROR` while `__init__.py` imported only `config_validation`,
`entity_registry` and `device_registry` - no `issue_registry as ir`. The call raised
NameError, the surrounding `except Exception` swallowed it into a debug line, and a user
running a retired or renamed profile key got no repair issue and no visible symptom: the
exact silent failure the block exists to prevent. Nothing caught it because that branch
never executes on a known profile, and no test imports `__init__.py` (it needs the full
Home Assistant entity stack).

A source scan does catch it, without importing anything. This is deliberately narrow -
a pyflakes-shaped check for *undefined* names only, not unused imports or shadowing -
because the point is to make an unreachable-in-tests branch fail loudly at review time.

Scope: the modules Home Assistant actually imports. `battery_sensors.py` is excluded and
must stay excluded - it is a copy-paste snippet ("Add these to SENSOR_DEFINITIONS in
sensor.py"), imported by nothing, and its 54 undefined names are its whole point.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"

# Everything reachable from manifest.json / the platform files. Kept explicit rather than
# globbed so adding a module is a deliberate act with a test consequence.
RUNTIME_MODULES = [
    "__init__.py",
    "auto_detection.py",
    "binary_sensor.py",
    "button.py",
    "config_flow.py",
    "const.py",
    "coordinator.py",
    "device_profiles.py",
    "diagnostic.py",
    "diagnostics.py",
    "entity.py",
    "growatt_modbus.py",
    "number.py",
    "select.py",
    "sensor.py",
    "switch.py",
    "time.py",
]

# Not imported by the integration; see the module docstring.
NOT_IMPORTED = {"battery_sensors.py"}


class _Bindings(ast.NodeVisitor):
    """Collect every name the module binds, anywhere, at any scope.

    Scope-insensitive on purpose. A name bound in one function and read in another is a
    different bug from the one this test is for, and modelling scopes properly means
    re-implementing pyflakes; conflating them only costs false negatives, never false
    positives.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)
        self.generic_visit(node)

    def _function(self, node) -> None:
        self.names.add(node.name)
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            self.names.add(arg.arg)
        if args.vararg:
            self.names.add(args.vararg.arg)
        if args.kwarg:
            self.names.add(args.kwarg.arg)
        self.generic_visit(node)

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            self.names.add(arg.arg)
        if args.vararg:
            self.names.add(args.vararg.arg)
        if args.kwarg:
            self.names.add(args.kwarg.arg)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.names.update(node.names)
        self.generic_visit(node)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.names.update(node.names)
        self.generic_visit(node)


def _undefined(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = _Bindings()
    bound.visit(tree)
    known = bound.names | set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        and node.id not in known
    }


@pytest.mark.parametrize("module", RUNTIME_MODULES)
def test_no_undefined_global_names(module: str) -> None:
    missing = sorted(_undefined(COMPONENT / module))
    assert not missing, (
        f"{module} uses {missing} without binding them. If the use sits inside a "
        f"try/except the failure is a swallowed NameError, not a crash."
    )


def test_the_unknown_profile_repair_issue_can_actually_be_raised() -> None:
    """The specific instance the scan above was written for.

    The block is guarded by `profile_exists()`, so it never runs on a supported profile
    and its `except Exception` hides a NameError as a debug line. Naming it here says what
    broke, which the generic scan cannot.
    """
    source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    assert "ir.async_create_issue(" in source, "the repair issue is gone; drop this test"
    assert "from homeassistant.helpers import issue_registry as ir" in source


def test_the_excluded_modules_are_still_not_imported() -> None:
    """The exclusion list is only safe while nothing imports those files."""
    for name in NOT_IMPORTED:
        stem = name[: -len(".py")]
        for module in RUNTIME_MODULES:
            source = (COMPONENT / module).read_text(encoding="utf-8")
            assert f"from .{stem} import" not in source, f"{module} imports {stem}"
            assert f"import {stem}" not in source.replace(f"import {stem}_", ""), (
                f"{module} imports {stem}"
            )
