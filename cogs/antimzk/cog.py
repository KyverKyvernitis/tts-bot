from .base import AntiMzkBase
from .focus import AntiMzkFocusMixin
from .slash_commands import AntiMzkCommandMixin
from .triggers import AntiMzkTriggerMixin


class AntiMzkCore(AntiMzkCommandMixin, AntiMzkFocusMixin, AntiMzkTriggerMixin, AntiMzkBase):
    pass
