from .base import AntiMzkBase
from .commands import AntiMzkCommandMixin
from .focus import AntiMzkFocusMixin
from .triggers import AntiMzkTriggerMixin


class AntiMzkCore(AntiMzkCommandMixin, AntiMzkFocusMixin, AntiMzkTriggerMixin, AntiMzkBase):
    pass
