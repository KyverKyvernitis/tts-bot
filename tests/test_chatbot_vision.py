"""Vision transport/preparation contracts; no live provider requests."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from cogs.chatbot import constants as C
from cogs.chatbot.media import (
    ImagePreparationError, MediaAttachment, PreparedImage,
    classify_attachment, prepare_image_attachments,
)
from cogs.chatbot.providers import (
    AllProvidersExhausted, ChatMessage, ProviderError, ProviderRouter,
    _GeminiClient, _GroqClient,
)


class _Body:
    def __init__(self, data: bytes):
        self.data = data

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]


class _Response:
    def __init__(self, body, *, status=200, mime="application/json", headers=None):
        self.status = status
        encoded = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.headers = {"Content-Type": mime, **(headers or {})}
        self.content = _Body(encoded)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, *, gets=(), posts=()):
        self.gets = list(gets)
        self.posts = list(posts)
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        response = self.gets.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.posts.pop(0)


def _png(size=(120, 80), *, compress_level=6, color=(255, 255, 255)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG", compress_level=compress_level)
    return output.getvalue()


def _attachment(data, *, url="https://cdn.discordapp.com/attachments/1/2/print.png?ex=signed", mime="image/png"):
    return MediaAttachment(url, "print.png", mime, len(data), "image")


def _gemini_text(text="li o print"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


@pytest.mark.asyncio
async def test_mime_parameters_and_generic_type_accept_decodable_screenshot():
    raw = _png()
    classified = classify_attachment(SimpleNamespace(
        content_type="image/png; charset=binary", filename="print.png", size=len(raw),
        url="https://cdn.discordapp.com/attachments/1/2/print.png",
    ))
    assert classified is not None and classified.mime_type == "image/png"
    generic = classify_attachment(SimpleNamespace(
        content_type="application/octet-stream", filename="print.png", size=len(raw), url=classified.url,
    ))
    assert generic is not None and generic.kind == "image"
    session = _Session(gets=[_Response(raw, mime="image/png; charset=binary")])
    images = await prepare_image_attachments(session, [classified])
    assert images[0].mime_type == "image/png"
    with Image.open(io.BytesIO(images[0].data)) as decoded:
        assert decoded.size == (120, 80)
        assert decoded.getpixel((30, 30)) == (255, 255, 255)


@pytest.mark.asyncio
async def test_raw_image_above_fallback_limit_is_normalized_before_provider():
    raw = _png((1800, 1800), compress_level=0)
    assert len(raw) > C.MAX_GEMINI_IMAGE_BYTES
    session = _Session(gets=[_Response(raw, mime="image/png")])
    images = await prepare_image_attachments(session, [_attachment(raw)])
    assert len(images[0].data) <= C.MAX_GEMINI_IMAGE_BYTES
    with Image.open(io.BytesIO(images[0].data)) as decoded:
        assert decoded.size == (1800, 1800)


@pytest.mark.asyncio
async def test_image_is_downloaded_once_and_same_bytes_reach_all_vision_fallbacks():
    raw = _png(color=(10, 40, 90))
    session = _Session(
        gets=[_Response(raw, mime="image/png")],
        posts=[
            _Response({"error": {"code": "model_not_found"}}, status=404),
            _Response({"error": {"message": "model unavailable"}}, status=404),
            _Response(_gemini_text()),
        ],
    )
    router = ProviderRouter(session, groq_key="test-groq", gemini_key="test-gemini")
    with patch.object(C, "GROQ_VISION_MODELS", ("groq-vision",)), patch.object(
        C, "GEMINI_VISION_MODELS", ("gemini-first", "gemini-second")
    ):
        result = await router.chat(system="s", messages=[ChatMessage("user", "leia", image_urls=[_attachment(raw).url])])
    assert result == "li o print"
    assert len(session.get_calls) == 1
    groq = session.post_calls[0][1]["json"]["messages"][-1]["content"][1]["image_url"]["url"]
    gemini_first = session.post_calls[1][1]["json"]["contents"][-1]["parts"][1]["inlineData"]
    gemini_second = session.post_calls[2][1]["json"]["contents"][-1]["parts"][1]["inlineData"]
    groq_bytes = base64.b64decode(groq.split(",", 1)[1])
    assert groq_bytes == base64.b64decode(gemini_first["data"]) == base64.b64decode(gemini_second["data"])
    assert gemini_first["mimeType"] == gemini_second["mimeType"] == "image/png"


@pytest.mark.asyncio
async def test_cdn_403_does_not_open_credential_circuit_or_log_signed_url(caplog):
    raw = _png()
    session = _Session(gets=[_Response(b"expired", status=403)], posts=[_Response(_gemini_text("texto ok"))])
    router = ProviderRouter(session, gemini_key="test-gemini")
    with pytest.raises(AllProvidersExhausted) as failure:
        await router.chat(system="s", messages=[ChatMessage("user", "leia", image_urls=[_attachment(raw).url])])
    assert failure.value.kind == "download"
    assert failure.value.stage == "attachment"
    assert failure.value.status == 403
    assert router.snapshot() == {}
    assert "signed" not in caplog.text and "attachments/" not in caplog.text
    assert await router.chat(system="s", messages=[ChatMessage("user", "oi")]) == "texto ok"


@pytest.mark.asyncio
async def test_gemini_legacy_client_cdn_403_is_attachment_error():
    session = _Session(gets=[_Response(b"expired", status=403)])
    with pytest.raises(ProviderError) as failure:
        await _GeminiClient(session, "test").chat(
            system="s", messages=[ChatMessage("user", "leia", image_urls=[_attachment(_png()).url])],
            temperature=.8, model="gemini-first", timeout_seconds=2,
        )
    assert failure.value.stage == "attachment" and failure.value.kind == "download"
    assert not session.post_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"promptFeedback": {"blockReason": "SAFETY"}},
    {"candidates": [{"finishReason": "SAFETY"}]},
])
async def test_policy_block_has_no_service_cooldown_and_is_not_retried(response):
    session = _Session(posts=[_Response(response)])
    router = ProviderRouter(session, gemini_key="test")
    with patch.object(C, "GEMINI_VISION_MODELS", ("first", "second")):
        with pytest.raises(AllProvidersExhausted) as failure:
            await router.chat(system="s", messages=[ChatMessage("user", "leia", images=[PreparedImage("image/png", _png())])])
    assert failure.value.kind == "blocked"
    assert len(session.post_calls) == 1
    assert router.snapshot()["gemini/first"]["available"]
    assert router.snapshot()["gemini/first"]["failures"] == 0


@pytest.mark.asyncio
async def test_explicit_refusal_as_text_is_returned_without_rewriting():
    session = _Session(posts=[_Response(_gemini_text("Não posso fazer isso."))])
    router = ProviderRouter(session, gemini_key="test")
    assert await router.chat(system="s", messages=[ChatMessage("user", "pedido")]) == "Não posso fazer isso."
    assert len(session.post_calls) == 1


@pytest.mark.asyncio
async def test_invalid_payload_does_not_open_model_circuit_and_error_body_is_not_logged(caplog):
    session = _Session(posts=[_Response({"error": {"message": "secret prompt invalid argument"}}, status=400)])
    router = ProviderRouter(session, gemini_key="test")
    with patch.object(C, "GEMINI_MODELS", ("first",)):
        with pytest.raises(AllProvidersExhausted) as failure:
            await router.chat(system="s", messages=[ChatMessage("user", "oi")])
    assert failure.value.kind == "request"
    assert router.snapshot()["gemini/first"]["available"]
    assert "secret prompt" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mime,raw,kind", [
    ("text/html", b"<html>error</html>", "mime"),
    ("image/png", b"not a PNG", "unreadable"),
])
async def test_actual_download_and_decode_are_validated(mime, raw, kind):
    session = _Session(gets=[_Response(raw, mime=mime)])
    with pytest.raises(ImagePreparationError) as failure:
        await prepare_image_attachments(session, [_attachment(raw)])
    assert failure.value.kind == kind


@pytest.mark.asyncio
async def test_pixel_cap_is_checked_before_decode():
    raw = _png((30, 30))
    session = _Session(gets=[_Response(raw, mime="image/png")])
    with patch.object(C, "MAX_VISION_IMAGE_PIXELS", 100):
        with pytest.raises(ImagePreparationError) as failure:
            await prepare_image_attachments(session, [_attachment(raw)])
    assert failure.value.kind == "size"


@pytest.mark.asyncio
async def test_download_timeout_is_typed_and_contains_no_url():
    session = _Session(gets=[asyncio.TimeoutError()])
    with pytest.raises(ImagePreparationError) as failure:
        await prepare_image_attachments(session, [_attachment(_png())])
    assert failure.value.kind == "timeout"
    assert "https" not in str(failure.value)


@pytest.mark.asyncio
async def test_preparation_timeout_is_bounded():
    raw = _png()
    session = _Session(gets=[_Response(raw, mime="image/png")])

    async def stalled_thread(*args, **kwargs):
        await asyncio.Future()

    with patch("cogs.chatbot.media.asyncio.to_thread", stalled_thread):
        with pytest.raises(ImagePreparationError) as failure:
            await prepare_image_attachments(session, [_attachment(raw)], timeout_seconds=.01)
    assert failure.value.kind == "timeout"


@pytest.mark.asyncio
async def test_animation_first_frame_and_transparency_are_explicit():
    output = io.BytesIO()
    frames = [Image.new("RGBA", (30, 20), color) for color in ("red", "blue")]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    raw = output.getvalue()
    session = _Session(gets=[_Response(raw, mime="image/gif")])
    images = await prepare_image_attachments(session, [_attachment(raw, mime="image/gif")])
    assert images[0].first_frame_only
    assert images[0].mime_type == "image/png"
    with Image.open(io.BytesIO(images[0].data)) as decoded:
        assert decoded.mode == "RGB" and decoded.getpixel((10, 10)) == (255, 0, 0)


@pytest.mark.asyncio
async def test_exif_orientation_is_applied_and_large_side_is_bounded():
    image = Image.new("RGB", (30, 10), "white")
    exif = image.getexif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, "JPEG", exif=exif)
    raw = output.getvalue()
    session = _Session(gets=[_Response(raw, mime="image/jpeg")])
    images = await prepare_image_attachments(session, [_attachment(raw, mime="image/jpeg")])
    with Image.open(io.BytesIO(images[0].data)) as decoded:
        assert decoded.size == (10, 30)
    raw = _png((200, 100))
    session = _Session(gets=[_Response(raw, mime="image/png")])
    with patch.object(C, "MAX_VISION_IMAGE_SIDE", 50):
        images = await prepare_image_attachments(session, [_attachment(raw)])
    with Image.open(io.BytesIO(images[0].data)) as decoded:
        assert decoded.size == (50, 25)


@pytest.mark.asyncio
async def test_batch_deduplicates_and_has_total_input_budget():
    raw = _png()
    session = _Session(gets=[_Response(raw, mime="image/png")])
    attachments = [_attachment(raw), _attachment(raw)]
    images = await prepare_image_attachments(session, attachments)
    assert len(images) == len(session.get_calls) == 1
    session = _Session(gets=[_Response(raw, mime="image/png")])
    with patch.object(C, "MAX_VISION_INPUT_TOTAL_BYTES", len(raw) + 1):
        with pytest.raises(ImagePreparationError) as failure:
            await prepare_image_attachments(session, [
                _attachment(raw), _attachment(raw, url="https://cdn.discordapp.com/attachments/1/2/other.png"),
            ])
    assert failure.value.kind == "size"


@pytest.mark.asyncio
async def test_redirect_cannot_leave_discord_and_is_not_logged():
    raw = _png()
    session = _Session(gets=[_Response(b"", status=302, headers={"Location": "https://example.com/image.png"})])
    with pytest.raises(ImagePreparationError) as failure:
        await prepare_image_attachments(session, [_attachment(raw)])
    assert failure.value.kind == "download"
    assert len(session.get_calls) == 1


@pytest.mark.asyncio
async def test_gemini_thinking_budget_and_output_limit_are_explicit():
    session = _Session(posts=[_Response(_gemini_text())])
    await _GeminiClient(session, "test").chat(
        system="s", messages=[ChatMessage("user", "leia", images=[PreparedImage("image/png", _png())])],
        temperature=.8, model="gemini-2.5-flash", timeout_seconds=2,
    )
    generation = session.post_calls[0][1]["json"]["generationConfig"]
    assert generation["thinkingConfig"] == {"thinkingBudget": 0}
    assert generation["maxOutputTokens"] == C.MAX_VISION_RESPONSE_TOKENS


@pytest.mark.asyncio
async def test_gemini_hidden_thoughts_are_not_sent_to_discord():
    response = _gemini_text()
    response["candidates"][0]["content"]["parts"].insert(0, {"text": "hidden chain of thought", "thought": True})
    session = _Session(posts=[_Response(response)])
    assert await _GeminiClient(session, "test").chat(
        system="s", messages=[ChatMessage("user", "oi")], temperature=.8,
        model="gemini-2.5-flash", timeout_seconds=2,
    ) == "li o print"


@pytest.mark.asyncio
async def test_empty_max_tokens_output_is_not_network_failure():
    session = _Session(posts=[_Response({"candidates": [{"finishReason": "MAX_TOKENS"}]})])
    with pytest.raises(ProviderError) as failure:
        await _GeminiClient(session, "test").chat(
            system="s", messages=[], temperature=.8, model="gemini-first", timeout_seconds=2,
        )
    assert failure.value.kind == "empty"
    assert failure.value.stage == "output" and failure.value.finish_reason == "MAX_TOKENS"


@pytest.mark.asyncio
async def test_groq_structured_content_filter_is_not_retried():
    session = _Session(posts=[_Response({"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]})])
    router = ProviderRouter(session, groq_key="test")
    with patch.object(C, "GROQ_MODELS", ("first", "second")):
        with pytest.raises(AllProvidersExhausted) as failure:
            await router.chat(system="s", messages=[ChatMessage("user", "oi")])
    assert failure.value.kind == "blocked"
    assert len(session.post_calls) == 1
    assert router.snapshot()["groq/first"]["available"]


@pytest.mark.asyncio
async def test_separate_groq_refusal_text_is_preserved_without_retry():
    response = {"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "Não posso fazer isso."}}]}
    session = _Session(posts=[_Response(response)])
    router = ProviderRouter(session, groq_key="test")
    with patch.object(C, "GROQ_MODELS", ("first", "second")):
        assert await router.chat(system="s", messages=[ChatMessage("user", "oi")]) == "Não posso fazer isso."
    assert len(session.post_calls) == 1


@pytest.mark.asyncio
async def test_empty_token_budget_does_not_cool_down_other_text_requests():
    session = _Session(posts=[_Response({"candidates": [{"finishReason": "MAX_TOKENS"}]})])
    router = ProviderRouter(session, gemini_key="test")
    with patch.object(C, "GEMINI_MODELS", ("first",)):
        with pytest.raises(AllProvidersExhausted) as failure:
            await router.chat(system="s", messages=[ChatMessage("user", "oi")])
    assert failure.value.kind == "empty" and failure.value.finish_reason == "MAX_TOKENS"
    assert router.snapshot()["gemini/first"]["available"]
    assert router.snapshot()["gemini/first"]["failures"] == 0


@pytest.mark.asyncio
async def test_model_entitlement_403_does_not_disable_entire_account():
    session = _Session(posts=[
        _Response({"error": {"code": "model_permission_denied", "message": "model not available to account"}}, status=403),
        _Response(_gemini_text("segundo ok")),
    ])
    router = ProviderRouter(session, gemini_key="test")
    with patch.object(C, "GEMINI_VISION_MODELS", ("first", "second")):
        assert await router.chat(
            system="s", messages=[ChatMessage("user", "leia", images=[PreparedImage("image/png", _png())])],
        ) == "segundo ok"
    assert len(session.post_calls) == 2
    assert not router.snapshot()["gemini/first"]["available"]
    assert router.snapshot()["gemini/second"]["available"]


def test_two_preparation_workers_share_one_decode_slot():
    import threading
    import time
    from cogs.chatbot import media

    first_entered = threading.Event()
    second_waiting = threading.Event()
    release_first = threading.Event()
    real_semaphore = threading.BoundedSemaphore(1)
    active = 0
    max_active = 0
    calls = []
    errors = []

    class ObservedSemaphore:
        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "second-image":
                second_waiting.set()
            return real_semaphore.acquire(*args, **kwargs)

        def release(self):
            real_semaphore.release()

    def decode(data, filename, *, byte_limit):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(filename)
        try:
            if filename == "first":
                first_entered.set()
                assert release_first.wait(timeout=2)
            return PreparedImage("image/png", b"prepared", filename)
        finally:
            active -= 1

    def worker(filename):
        try:
            media._prepare_image_bytes(b"raw", filename, byte_limit=100, deadline=time.monotonic() + 2)
        except Exception as exc:
            errors.append(exc)

    with patch.object(media, "_IMAGE_PREPARATION_SLOTS", ObservedSemaphore()), patch.object(media, "_decode_image_bytes", decode):
        first = threading.Thread(target=worker, args=("first",), name="first-image")
        second = threading.Thread(target=worker, args=("second",), name="second-image")
        first.start()
        try:
            assert first_entered.wait(timeout=1)
            second.start()
            assert second_waiting.wait(timeout=1)
            assert calls == ["first"]
        finally:
            release_first.set()
            first.join(timeout=2)
            if second.ident is not None:
                second.join(timeout=2)
    assert not errors
    assert calls == ["first", "second"]
    assert max_active == 1


def test_expired_worker_never_starts_second_decode():
    import threading
    import time
    from cogs.chatbot import media

    first_entered = threading.Event()
    release_first = threading.Event()
    calls = []
    errors = []

    def decode(data, filename, *, byte_limit):
        calls.append(filename)
        first_entered.set()
        assert release_first.wait(timeout=2)
        return PreparedImage("image/png", b"prepared", filename)

    def worker(filename, timeout):
        try:
            media._prepare_image_bytes(b"raw", filename, byte_limit=100, deadline=time.monotonic() + timeout)
        except Exception as exc:
            errors.append((filename, exc))

    with patch.object(media, "_decode_image_bytes", decode):
        first = threading.Thread(target=worker, args=("first", 2))
        second = threading.Thread(target=worker, args=("second", .02))
        first.start()
        try:
            assert first_entered.wait(timeout=1)
            second.start()
            second.join(timeout=1)
            assert not second.is_alive()
            assert calls == ["first"]
            assert len(errors) == 1 and errors[0][0] == "second"
            assert isinstance(errors[0][1], ImagePreparationError)
            assert errors[0][1].kind == "timeout"
        finally:
            release_first.set()
            first.join(timeout=2)
            if second.ident is not None:
                second.join(timeout=2)
    assert calls == ["first"]


@pytest.mark.asyncio
async def test_cancelled_caller_keeps_slot_until_worker_finishes():
    import threading
    import time
    from cogs.chatbot import media

    first_entered = threading.Event()
    release_first = threading.Event()
    calls = []

    def decode(data, filename, *, byte_limit):
        calls.append(filename)
        first_entered.set()
        assert release_first.wait(timeout=2)
        return PreparedImage("image/png", b"prepared", filename)

    with patch.object(media, "_decode_image_bytes", decode):
        task = asyncio.create_task(asyncio.to_thread(
            media._prepare_image_bytes, b"raw", "first", byte_limit=100, deadline=time.monotonic() + 2,
        ))
        try:
            assert await asyncio.to_thread(first_entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ImagePreparationError) as failure:
                await asyncio.to_thread(
                    media._prepare_image_bytes, b"raw", "second", byte_limit=100,
                    deadline=time.monotonic() + .02,
                )
            assert failure.value.kind == "timeout"
            assert calls == ["first"]
        finally:
            release_first.set()
            # A slot acquisition confirms that the cancelled worker actually
            # completed before replacing its mocked decoder in the next test.
            assert await asyncio.to_thread(media._IMAGE_PREPARATION_SLOTS.acquire, True, 1)
            media._IMAGE_PREPARATION_SLOTS.release()


@pytest.mark.asyncio
async def test_qwen38_vision_disables_hidden_reasoning_using_supported_arguments():
    session = _Session(posts=[_Response({"choices": [{"finish_reason": "stop", "message": {"content": "li o print"}}]})])
    result = await _GroqClient(session, "test").chat(
        system="s", messages=[ChatMessage("user", "leia", images=[PreparedImage("image/png", _png())])],
        temperature=.8, model="qwen/qwen3.8-27b", timeout_seconds=2,
    )
    assert result == "li o print"
    payload = session.post_calls[0][1]["json"]
    assert payload["reasoning_effort"] == "none"
    assert payload["include_reasoning"] is False
    assert "reasoning_format" not in payload
    assert payload["max_completion_tokens"] == C.MAX_VISION_RESPONSE_TOKENS
