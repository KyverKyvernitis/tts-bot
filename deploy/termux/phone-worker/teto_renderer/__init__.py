from .errors import (
    TetoConfigurationError,
    TetoRendererError,
    TetoResourceError,
    TetoSynthesisError,
    TetoVoicebankError,
)
from .renderer import TetoRenderer


def __getattr__(name):
    # An existing UTAU install can load its renderer while an updater is still
    # staging the optional WORLDLINE modules.
    if name == "WorldlineRenderer":
        from .worldline import WorldlineRenderer
        return WorldlineRenderer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "TetoRenderer",
    "WorldlineRenderer",
    "TetoRendererError",
    "TetoConfigurationError",
    "TetoVoicebankError",
    "TetoResourceError",
    "TetoSynthesisError",
]
