from .base import AntiMzkBase
from .focus import AntiMzkFocusMixin
from .poker import AntiMzkPokerMixin
from .slash_commands import AntiMzkCommandMixin
from .triggers import AntiMzkTriggerMixin


class AntiMzkCore(AntiMzkCommandMixin, AntiMzkFocusMixin, AntiMzkPokerMixin, AntiMzkTriggerMixin, AntiMzkBase):
    pass
