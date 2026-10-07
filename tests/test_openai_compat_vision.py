"""R7: contratos de input multimodal, com casos sem Flask/Discord."""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from utility.openai_compat_vision import (
    InlineImageError, MAX_IMAGE_INPUT_BYTES, parse_inline_image_url,
)


def _png(size=(4, 4), color=(20, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _uri(data, mime="image/png"):
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def test_r7_ogam_openai_image_url_is_decoded():
    picture = _png()
    assert parse_inline_image_url({"url": _uri(picture), "detail": "auto"}) == picture
    assert parse_inline_image_url(_uri(picture)) == picture


def test_r7_refuses_remote_urls_instead_of_fetching_them():
    for url in ("https://example.com/x.png", "http://127.0.0.1:8080/pic.png", "file:///etc/passwd"):
        with pytest.raises(InlineImageError) as err:
            parse_inline_image_url({"url": url})
        assert err.value.code == "unsupported_image_source"


@pytest.mark.parametrize("value", [None, 55, {}, "data:image/svg+xml;base64,aGVsbG8=", "data:image/png;base64,Zg==", "data:image/png;base64,---="])
def test_r7_refuses_invalid_image_payload(value):
    with pytest.raises(InlineImageError):
        parse_inline_image_url(value)


def test_r7_rejects_too_large_before_decoding():
    uri = _uri(_png())
    with pytest.raises(InlineImageError) as err:
        parse_inline_image_url(uri, max_bytes=4)
    assert err.value.code == "image_too_large"
    huge_uri = "data:image/png;base64," + ("A" * (((MAX_IMAGE_INPUT_BYTES + 3) // 3) * 4 + 256))
    with pytest.raises(InlineImageError) as err:
        parse_inline_image_url(huge_uri)
    assert err.value.code == "image_too_large"


def test_r7_rejects_mime_spoofing_before_pillow():
    with pytest.raises(InlineImageError) as err:
        parse_inline_image_url(_uri(_png(), mime="image/jpeg"))
    assert err.value.code == "invalid_image_data"


def test_r7_supported_signatures_not_generic_payloads():
    with pytest.raises(InlineImageError) as err:
        parse_inline_image_url("data:image/webp;base64," + base64.b64encode(b"RIFF0000AAAA").decode())
    assert err.value.code == "invalid_image_data"


@pytest.mark.asyncio
async def test_r7_existing_image_preparation_pipeline_validates_pixels_and_format():
    pytest.importorskip("discord")
    from cogs.chatbot.media import prepare_openai_inline_image, ImagePreparationError
    raw = _png()
    result = await prepare_openai_inline_image(raw)
    assert result.mime_type == "image/png"
    assert result.data.startswith(b"\x89PNG")
    with pytest.raises(ImagePreparationError) as error:
        await prepare_openai_inline_image(b"\x89PNG\r\n\x1a\nINVALID")
    assert error.value.kind == "unreadable"


@pytest.mark.asyncio
async def test_r7_limits_pixel_count_even_when_png_compresses_well():
    pytest.importorskip("discord")
    from cogs.chatbot.media import prepare_openai_inline_image, ImagePreparationError
    # 13 milhões de pixels: supera o cap de 12 MP, mesmo comprimido <4 MiB.
    raw = _png(size=(4000, 3250))
    assert len(raw) < MAX_IMAGE_INPUT_BYTES
    with pytest.raises(ImagePreparationError) as error:
        await prepare_openai_inline_image(raw)
    assert error.value.kind == "size"
