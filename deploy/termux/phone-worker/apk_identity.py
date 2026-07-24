"""Leitura estrita da identidade compilada de um APK sem depender do Android SDK."""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ANDROID_NS = "http://schemas.android.com/apk/res/android"
NO_INDEX = 0xFFFFFFFF
RES_STRING_POOL_TYPE = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_RESOURCE_MAP_TYPE = 0x0180
RES_XML_START_ELEMENT_TYPE = 0x0102
UTF8_FLAG = 0x00000100
TYPE_STRING = 0x03
TYPE_FIRST_INT = 0x10
TYPE_LAST_INT = 0x1F
ANDROID_VERSION_CODE_ID = 0x0101021B
ANDROID_VERSION_NAME_ID = 0x0101021C
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class ApkIdentityError(ValueError):
    """O APK não contém uma identidade Android compilada válida e verificável."""


def _require(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ApkIdentityError(f"AndroidManifest.xml truncado em {label}")


def _u16(data: bytes, offset: int, label: str = "u16") -> int:
    _require(data, offset, 2, label)
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int, label: str = "u32") -> int:
    _require(data, offset, 4, label)
    return struct.unpack_from("<I", data, offset)[0]


def _decode_length8(data: bytes, offset: int) -> tuple[int, int]:
    _require(data, offset, 1, "comprimento UTF-8")
    first = data[offset]
    if first & 0x80:
        _require(data, offset, 2, "comprimento UTF-8 longo")
        return ((first & 0x7F) << 8) | data[offset + 1], offset + 2
    return first, offset + 1


def _decode_length16(data: bytes, offset: int) -> tuple[int, int]:
    first = _u16(data, offset, "comprimento UTF-16")
    if first & 0x8000:
        second = _u16(data, offset + 2, "comprimento UTF-16 longo")
        return ((first & 0x7FFF) << 16) | second, offset + 4
    return first, offset + 2


def _read_string_pool(data: bytes, chunk_offset: int, header_size: int, chunk_size: int) -> list[str]:
    if header_size < 28:
        raise ApkIdentityError("string pool com header inválido")
    string_count = _u32(data, chunk_offset + 8, "stringCount")
    style_count = _u32(data, chunk_offset + 12, "styleCount")
    flags = _u32(data, chunk_offset + 16, "string flags")
    strings_start = _u32(data, chunk_offset + 20, "stringsStart")
    styles_start = _u32(data, chunk_offset + 24, "stylesStart")
    if string_count > 1_000_000 or style_count > 1_000_000:
        raise ApkIdentityError("string pool excessivo")
    offsets_start = chunk_offset + header_size
    offsets_bytes = (string_count + style_count) * 4
    _require(data, offsets_start, offsets_bytes, "offsets da string pool")
    string_data_start = chunk_offset + strings_start
    chunk_end = chunk_offset + chunk_size
    string_data_end = chunk_offset + styles_start if styles_start else chunk_end
    if string_data_start < offsets_start or string_data_start > string_data_end or string_data_end > chunk_end:
        raise ApkIdentityError("faixa inválida da string pool")

    utf8 = bool(flags & UTF8_FLAG)
    strings: list[str] = []
    for index in range(string_count):
        relative = _u32(data, offsets_start + index * 4, "offset de string")
        cursor = string_data_start + relative
        if cursor < string_data_start or cursor >= string_data_end:
            raise ApkIdentityError("offset de string fora da string pool")
        if utf8:
            _utf16_length, cursor = _decode_length8(data, cursor)
            byte_length, cursor = _decode_length8(data, cursor)
            _require(data, cursor, byte_length + 1, "string UTF-8")
            raw = data[cursor:cursor + byte_length]
            if data[cursor + byte_length] != 0:
                raise ApkIdentityError("string UTF-8 sem terminador")
            strings.append(raw.decode("utf-8", errors="strict"))
        else:
            char_length, cursor = _decode_length16(data, cursor)
            byte_length = char_length * 2
            _require(data, cursor, byte_length + 2, "string UTF-16")
            raw = data[cursor:cursor + byte_length]
            if data[cursor + byte_length:cursor + byte_length + 2] != b"\0\0":
                raise ApkIdentityError("string UTF-16 sem terminador")
            strings.append(raw.decode("utf-16le", errors="strict"))
    return strings


def _pool_string(strings: list[str], index: int) -> str:
    if index == NO_INDEX:
        return ""
    if index < 0 or index >= len(strings):
        raise ApkIdentityError("índice de string inválido no manifest")
    return strings[index]


def _typed_value(strings: list[str], raw_index: int, data_type: int, value_data: int) -> Any:
    if raw_index != NO_INDEX:
        return _pool_string(strings, raw_index)
    if data_type == TYPE_STRING:
        return _pool_string(strings, value_data)
    if TYPE_FIRST_INT <= data_type <= TYPE_LAST_INT:
        return int(value_data)
    return None


def _parse_text_manifest(raw: bytes) -> dict[str, Any]:
    try:
        root = ElementTree.fromstring(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ApkIdentityError(f"AndroidManifest.xml textual inválido: {type(exc).__name__}") from exc
    if root.tag.rsplit("}", 1)[-1] != "manifest":
        raise ApkIdentityError("raiz do AndroidManifest.xml não é manifest")
    package_name = str(root.attrib.get("package") or "").strip()
    version_name = str(root.attrib.get(f"{{{ANDROID_NS}}}versionName") or "").strip()
    raw_code = root.attrib.get(f"{{{ANDROID_NS}}}versionCode")
    try:
        version_code = int(str(raw_code or "0"), 0)
    except Exception as exc:
        raise ApkIdentityError("versionCode textual inválido") from exc
    return _finish_identity(package_name, version_name, version_code)


def _finish_identity(package_name: str, version_name: str, version_code: int) -> dict[str, Any]:
    package_name = str(package_name or "").strip()
    version_name = str(version_name or "").strip()
    try:
        version_code = int(version_code)
    except Exception as exc:
        raise ApkIdentityError("versionCode compilado inválido") from exc
    if not package_name:
        raise ApkIdentityError("package ausente no AndroidManifest.xml compilado")
    if not version_name:
        raise ApkIdentityError("versionName ausente ou não resolvível no APK")
    if version_code <= 0:
        raise ApkIdentityError("versionCode ausente ou inválido no APK")
    return {
        "packageName": package_name,
        "versionName": version_name,
        "versionCode": version_code,
    }


def parse_android_manifest_identity(raw: bytes) -> dict[str, Any]:
    """Extrai package/versionName/versionCode do manifest textual ou AXML binário."""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ApkIdentityError("AndroidManifest.xml vazio")
    data = bytes(raw)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ApkIdentityError("AndroidManifest.xml excede o limite")
    if data.lstrip().startswith(b"<"):
        return _parse_text_manifest(data)

    if len(data) < 8 or _u16(data, 0, "tipo XML") != RES_XML_TYPE:
        raise ApkIdentityError("AndroidManifest.xml não é AXML binário")
    root_header_size = _u16(data, 2, "header XML")
    root_size = _u32(data, 4, "tamanho XML")
    if root_header_size < 8 or root_size < root_header_size or root_size > len(data):
        raise ApkIdentityError("header AXML inválido")

    strings: list[str] = []
    resource_map: list[int] = []
    offset = root_header_size
    while offset < root_size:
        _require(data, offset, 8, "chunk AXML")
        chunk_type = _u16(data, offset, "tipo de chunk")
        header_size = _u16(data, offset + 2, "header de chunk")
        chunk_size = _u32(data, offset + 4, "tamanho de chunk")
        if header_size < 8 or chunk_size < header_size or offset + chunk_size > root_size:
            raise ApkIdentityError("chunk AXML inválido")
        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _read_string_pool(data, offset, header_size, chunk_size)
        elif chunk_type == RES_XML_RESOURCE_MAP_TYPE:
            count = (chunk_size - header_size) // 4
            resource_map = [_u32(data, offset + header_size + index * 4, "resource map") for index in range(count)]
        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            if not strings:
                raise ApkIdentityError("elemento AXML antes da string pool")
            if header_size < 16 or chunk_size < 36:
                raise ApkIdentityError("start element AXML inválido")
            ext = offset + 16
            element_name_index = _u32(data, ext + 4, "nome do elemento")
            if _pool_string(strings, element_name_index) != "manifest":
                offset += chunk_size
                continue
            attribute_start = _u16(data, ext + 8, "attributeStart")
            attribute_size = _u16(data, ext + 10, "attributeSize")
            attribute_count = _u16(data, ext + 12, "attributeCount")
            if attribute_size < 20 or attribute_count > 4096:
                raise ApkIdentityError("atributos AXML inválidos")
            attrs_offset = ext + attribute_start
            attrs_end = attrs_offset + attribute_count * attribute_size
            if attrs_offset < ext or attrs_end > offset + chunk_size:
                raise ApkIdentityError("faixa de atributos AXML inválida")

            package_name = ""
            version_name = ""
            version_code = 0
            for index in range(attribute_count):
                attr = attrs_offset + index * attribute_size
                namespace_index = _u32(data, attr, "namespace do atributo")
                name_index = _u32(data, attr + 4, "nome do atributo")
                raw_index = _u32(data, attr + 8, "valor bruto do atributo")
                typed_size = _u16(data, attr + 12, "typed value size")
                data_type = data[attr + 15]
                value_data = _u32(data, attr + 16, "typed value data")
                if typed_size < 8:
                    raise ApkIdentityError("typed value inválido no AXML")
                name = _pool_string(strings, name_index)
                namespace = _pool_string(strings, namespace_index)
                resource_id = resource_map[name_index] if 0 <= name_index < len(resource_map) else 0
                value = _typed_value(strings, raw_index, data_type, value_data)
                if name == "package" and not namespace:
                    package_name = str(value or "").strip()
                elif resource_id == ANDROID_VERSION_CODE_ID or (name == "versionCode" and namespace == ANDROID_NS):
                    if isinstance(value, int):
                        version_code = value
                    elif value is not None:
                        try:
                            version_code = int(str(value), 0)
                        except Exception as exc:
                            raise ApkIdentityError("versionCode compilado não é inteiro") from exc
                elif resource_id == ANDROID_VERSION_NAME_ID or (name == "versionName" and namespace == ANDROID_NS):
                    if value is not None:
                        version_name = str(value).strip()
            return _finish_identity(package_name, version_name, version_code)
        offset += chunk_size
    raise ApkIdentityError("elemento manifest não encontrado no AXML")


def inspect_apk_identity(apk_path: str | Path) -> dict[str, Any]:
    """Lê a identidade real do APK e valida a integridade mínima do arquivo ZIP."""
    path = Path(apk_path)
    if not path.is_file():
        raise ApkIdentityError("APK não encontrado")
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ApkIdentityError(f"APK corrompido em {bad}")
            names = set(archive.namelist())
            if "AndroidManifest.xml" not in names or "classes.dex" not in names:
                raise ApkIdentityError("arquivo não contém manifest/classes de APK")
            info = archive.getinfo("AndroidManifest.xml")
            if int(info.file_size or 0) <= 0 or int(info.file_size or 0) > MAX_MANIFEST_BYTES:
                raise ApkIdentityError("AndroidManifest.xml com tamanho inválido")
            raw = archive.read("AndroidManifest.xml")
    except ApkIdentityError:
        raise
    except Exception as exc:
        raise ApkIdentityError(f"APK inválido: {type(exc).__name__}: {exc}") from exc
    return parse_android_manifest_identity(raw)


def assert_expected_apk_identity(
    identity: dict[str, Any],
    *,
    expected_package: str = "dev.core.worker",
    expected_version_name: str = "",
    expected_version_code: int = 0,
) -> dict[str, Any]:
    """Reprova metadados externos que tentem renomear um APK diferente."""
    package_name = str(identity.get("packageName") or "").strip()
    version_name = str(identity.get("versionName") or "").strip()
    version_code = int(identity.get("versionCode") or 0)
    if expected_package and package_name != expected_package:
        raise ApkIdentityError(f"package do APK divergente: {package_name or '?'} != {expected_package}")
    if expected_version_name and version_name != str(expected_version_name).strip():
        raise ApkIdentityError(f"versionName do APK divergente: binário={version_name or '?'} solicitado={expected_version_name}")
    if int(expected_version_code or 0) > 0 and version_code != int(expected_version_code):
        raise ApkIdentityError(f"versionCode do APK divergente: binário={version_code} solicitado={int(expected_version_code)}")
    return identity
