"""Check a module never imports the protoAgent host, by parsing it rather than grepping strings."""

from __future__ import annotations

import ast
from pathlib import Path

HOST_PACKAGES = ("graph", "server", "operator_api", "runtime")


def host_imports(path: Path) -> list[str]:
    """Every host package the module imports: plain and from-imports at any depth, plus
    ``importlib.import_module("...")`` and ``__import__("...")`` calls with a literal name."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found: list[str] = []

    def host(name: str | None) -> bool:
        return bool(name) and name.split(".")[0] in HOST_PACKAGES

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if host(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and host(node.module):
            found.append(node.module)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if (
                name in ("import_module", "__import__")
                and isinstance(node.args[0].value, str)
                and host(node.args[0].value)
            ):
                found.append(node.args[0].value)
    return found
