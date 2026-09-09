"""Tool surface — aggregator.

The tools now live in focused modules: the shared engine and the mcp instance in
`_core`, each tool in its own module under `tools/`. Importing them here runs
their `@mcp.tool` registrations and re-exports the shared engine, so
`from .server import mcp` and the test helper imports keep working unchanged.
"""
from ._core import *  # noqa: F401,F403  — mcp instance and shared engine/plumbing
from ._core import mcp  # noqa: F401
from . import tools  # noqa: F401  — importing the package registers every tool
# Backward-compatible re-exports of tool-local helpers used by the tests.
from .tools.reading import _page_status  # noqa: F401
from .tools.aggregate import _parse_measures, _resolve_filename  # noqa: F401
from .tools.compare import _natural_key, _identity_column  # noqa: F401
