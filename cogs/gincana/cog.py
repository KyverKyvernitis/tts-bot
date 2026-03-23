from .commands.admin import GincanaCommandMixin
from .games.alvo import GincanaAlvoMixin
from .games.buckshot import GincanaBuckshotMixin
from .games.poker import GincanaPokerMixin
from .games.roleta import GincanaRoletaMixin
from .handlers.focus import GincanaFocusMixin
from .handlers.message_router import GincanaMessageRouterMixin
from .services.audio import GincanaAudioMixin
from .services.base import GincanaBase
from .services.toggles import GincanaToggleMixin


class GincanaCore(
    GincanaCommandMixin,
    GincanaFocusMixin,
    GincanaPokerMixin,
    GincanaMessageRouterMixin,
    GincanaRoletaMixin,
    GincanaBuckshotMixin,
    GincanaAlvoMixin,
    GincanaToggleMixin,
    GincanaAudioMixin,
    GincanaBase,
):
    pass
