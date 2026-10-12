"""Compatibility endpoint for edge-tts 7.2.8 synthesis.

The legacy speech.platform.bing.com WebSocket returns HTTP 403; the
api.msedgeservices.com endpoint has been verified to return audio using the
same edge-tts version and subscription token. Only the synthesis WebSocket is
changed. Voice-list requests and user preferences are untouched.
"""

_LEGACY_PREFIX = (
    "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?"
)
_NEW_ENDPOINT = "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"


def configure_edge_endpoint() -> bool:
    """Configure the shared edge-tts Communicate transport once per process.

    Skip unknown/future transports instead of overwriting a library update.
    The project's unit tests may substitute a minimal edge_tts stub.
    """
    try:
        from edge_tts import communicate
        from edge_tts.constants import TRUSTED_CLIENT_TOKEN
    except (ImportError, AttributeError):
        return False

    existing = getattr(communicate, "WSS_URL", None)
    if not isinstance(existing, str):
        return False
    if existing.startswith(_NEW_ENDPOINT + "?Ocp-Apim-Subscription-Key="):
        return True
    if not existing.startswith(_LEGACY_PREFIX):
        return False
    if not isinstance(TRUSTED_CLIENT_TOKEN, str) or not TRUSTED_CLIENT_TOKEN:
        return False

    communicate.WSS_URL = (
        _NEW_ENDPOINT + "?Ocp-Apim-Subscription-Key=" + TRUSTED_CLIENT_TOKEN
    )
    return True
