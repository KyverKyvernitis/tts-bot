"""Classify confirmed worker rejections without changing the singing backend."""
from __future__ import annotations

import re


_LYRICS_SHORT = (
    "a letra não encaixa nas frases do midi",
    "letra com ~ ocupa",
    "~ não pode prolongar uma vogal através de uma pausa midi",
    "~ precisa vir depois de uma sílaba",
    "a letra não contém vogais que a voz possa cantar",
    "uma vogal contínua pode durar até 18 segundos",
    "melisma abrange mais de 40 semitons",
    "salto de notas grande demais para um único trecho",
    "cada letra midi deve conter uma sílaba",
    "a letra precisa conter uma vogal e terminar em uma sílaba",
)
_SCORE_ERRORS = (
    "partitura ttv inválida", "versão da partitura ttv não suportada",
    "faixa midi inválida", "origem da letra midi inválida",
    "midi deve durar até 60 segundos", "midi deve conter entre 1 e 128 notas",
    "nota midi inválida", "notas midi devem estar entre c2 e c7",
    "intensidade midi inválida", "tempo da nota midi inválido",
    "midi exige uma melodia sem notas sobrepostas", "letra incorporada ao midi inválida",
    "canto midi preserva tom e andamento", "hash da apresentação ttv",
)
_BUSY = ("renderer ttv ocupado", "renderer teto ocupado", "tts agent ocupado",
         "fila local cheia", "tarefa pesada bloqueada", "bateria baixa")
_REQUEST_CODES = frozenset({"ttv_singing_lyrics_mismatch", "ttv_singing_lyrics_too_long",
                           "ttv_singing_score_invalid", "ttv_singing_busy",
                           "ttv_singing_bank_missing", "ttv_singing_backend_unsupported"})


def singing_failure_code(error: BaseException | str) -> str:
    """Return only fixed public categories; never return remote error text."""
    if isinstance(error, (TimeoutError,)):
        return "ttv_singing_timeout"
    text = str(error).casefold()
    if text.startswith("tetoresourceerror:"):
        return "ttv_singing_busy"
    if any(token in text for token in _LYRICS_SHORT):
        return "ttv_singing_lyrics_mismatch"
    if re.search(r"letra contém \d+ sílabas para \d+ notas", text):
        return "ttv_singing_lyrics_too_long"
    if any(token in text for token in _SCORE_ERRORS):
        return "ttv_singing_score_invalid"
    if any(token in text for token in _BUSY):
        return "ttv_singing_busy"
    if "canto midi exige esper" in text or "worldline ainda não aceita partituras" in text:
        return "ttv_singing_backend_unsupported"
    if ("english-cvvc" in text and ("indisponível" in text or "exige" in text)):
        return "ttv_singing_bank_missing"
    return "ttv_singing_failed"


class PhoneWorkerResponseError(RuntimeError):
    """An explicit JSON rejection, separate from transport or invalid audio."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.worker_category = singing_failure_code(message)
        match = re.match(r"([A-Za-z][A-Za-z0-9_]{0,63}):", message)
        self.worker_exception = match.group(1) if match else ""


def request_scoped_worker_rejection(error: BaseException | str) -> bool:
    """Only a decoded worker response can exempt a failure from circuit breaking.

    A network exception that happens to contain similar text still represents
    a failed route. Request validation, voice-specific configuration and busy
    responses demonstrate a functioning transport and do not disable every voice.
    """
    return (isinstance(error, PhoneWorkerResponseError)
            and error.status not in {401, 403}
            and error.worker_category in _REQUEST_CODES)
