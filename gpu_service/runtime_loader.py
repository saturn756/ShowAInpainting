"""Select a model runtime without coupling the API layer to research code."""

from __future__ import annotations

import importlib
import sys
from typing import Any

try:
    from .config import Settings
    from .mock_runtime import MockRuntime
except ImportError:
    from config import Settings
    from mock_runtime import MockRuntime


def load_runtime(settings: Settings) -> Any:
    if settings.runtime.backend == "mock" or settings.runtime.smoke_mode:
        return MockRuntime()

    if settings.runtime.module_path:
        module_path = str(settings.runtime.module_path)
        if module_path not in sys.path:
            sys.path.insert(0, module_path)

    module = importlib.import_module(settings.runtime.module)
    runtime_class = getattr(module, "ModelRuntime", None)
    if runtime_class is None:
        raise RuntimeError(
            f"runtime module {settings.runtime.module!r} must export ModelRuntime"
        )

    # The private adapter owns its own model configuration. The public API
    # passes only the typed service settings and never knows model parameters.
    return runtime_class(settings=settings)
