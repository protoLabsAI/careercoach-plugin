"""Test bootstrap — load the plugin host-free (no protoAgent running).

Uses the vendored testkit (``_plugin_testkit.py`` — a verbatim copy of protoAgent's
``graph/plugins/testkit.py``, dropped here so the suite has zero protoAgent dependency).
It loads the plugin as a PACKAGE, so your tests can import and exercise the REAL sibling
modules — relative imports and all — not just ``register()``:

    def test_engine(plugin):
        import importlib
        engine = importlib.import_module(plugin.__name__ + ".engine")  # a deep module
        assert engine.classify([...]) == ...

``install_host_stubs()`` (run at import below) registers stand-ins for the host-only
modules (``graph.*`` / ``knowledge.*``) so a plugin module that imports them loads with no
host; monkeypatch a seam to assert its behaviour. To refresh the vendored copy after a
protoAgent upgrade, re-run ``scaffold_plugin`` or recopy ``graph/plugins/testkit.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `import _plugin_testkit` resolves

from _plugin_testkit import FakeRegistry, install_host_stubs, load_plugin  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ID = "careercoach"

install_host_stubs()  # before any plugin module imports the host


@pytest.fixture
def plugin():
    """The plugin loaded as a package — ``register()`` runs and ``from <pkg> import x``
    works, so you can unit-test deep engine modules, not just registration."""
    return load_plugin(ROOT, PLUGIN_ID)


@pytest.fixture
def registry():
    """A fake registry that records what ``register()`` contributes (assert against it)."""
    return FakeRegistry()
