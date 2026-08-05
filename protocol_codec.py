"""Metadata-driven encoder for the local SNet wire format."""

from __future__ import annotations

import json
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parent
METADATA = json.loads((ROOT / "analysis" / "net_metadata.json").read_text(encoding="utf-8"))
METHODS = {entry["message_id"]: entry for entry in METADATA["methods"]}
POD_TYPES = METADATA["pod_types"]
POD_SERIAL_IDS = METADATA["pod_serial_ids"]


def encode_int(value: int) -> bytes:
    if value == -1:
        return b"\x5f\xff\xff\xff\xff"
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("int must fit in unsigned 32-bit protocol range")
    if value == 0:
        return b"\x50"
    raw = value.to_bytes(4, "little")
    mask = sum(1 << index for index, byte in enumerate(raw) if byte)
    return bytes([0x50 | mask]) + bytes(byte for byte in raw if byte)


def encode_count(count: int, base: int) -> bytes:
    if not 0 <= count <= 0xFFFFFFFF:
        raise ValueError("collection count is outside protocol range")
    if count == 0:
        return bytes([base])
    raw = count.to_bytes(4, "little")
    mask = sum(1 << index for index, byte in enumerate(raw) if byte)
    return bytes([base | mask]) + bytes(byte for byte in raw if byte)


def split_generic(type_name: str) -> tuple[str, str] | None:
    if "<" not in type_name or not type_name.endswith(">"):
        return None
    outer, inner = type_name.split("<", 1)
    return outer, inner[:-1]


def encode_value(type_name: str, value) -> bytes:
    if type_name == "int":
        return encode_int(value)
    if type_name == "long":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("long field requires an integer")
        raw = value.to_bytes(8, "little", signed=value < 0)
        mask = sum(1 << index for index, byte in enumerate(raw) if byte)
        return b"\x90" + bytes([mask]) + bytes(byte for byte in raw if byte)
    if type_name == "bool":
        if not isinstance(value, bool):
            raise ValueError("bool field requires a bool value")
        return b"\x01" if value else b"\x00"
    if type_name == "string":
        raw = value.encode("utf-8")
        return encode_count(len(raw), 0xA0) + raw
    if type_name in ("float", "double"):
        raw = struct.pack("<f" if type_name == "float" else "<d", value)
        mask = sum(1 << index for index, byte in enumerate(raw) if byte)
        base = 0x70 if type_name == "float" else 0x80
        return bytes([base, mask]) + bytes(byte for byte in raw if byte)

    generic = split_generic(type_name)
    if generic:
        outer, inner = generic
        if outer == "list":
            return encode_count(len(value), 0xD0) + b"".join(
                encode_value(inner, item) for item in value
            )
        if outer == "map":
            key_type, value_type = inner.split("|", 1)
            return encode_count(len(value), 0xC0) + b"".join(
                encode_value(key_type, key) + encode_value(value_type, item)
                for key, item in sorted(value.items())
            )

    if type_name in POD_TYPES:
        field_types = POD_TYPES[type_name]
        serial_ids = POD_SERIAL_IDS[type_name]
        present = [name for name in field_types if name in value and value[name] is not None]
        present.sort(key=serial_ids.__getitem__)
        body = bytearray(encode_count(len(present), 0xC0))
        for name in present:
            body.extend(encode_int(serial_ids[name]))
            body.extend(encode_value(field_types[name], value[name]))
        return bytes(body)
    raise ValueError(f"unsupported protocol type: {type_name}")


def encode_method(message_id: int, *values) -> bytes:
    method = METHODS.get(message_id)
    if method is None:
        raise KeyError(f"unknown message id {message_id}")
    types = method["types"]
    if len(types) != len(values):
        raise ValueError(f"message {message_id} expects {len(types)} values, got {len(values)}")
    return b"".join(encode_value(type_name, value) for type_name, value in zip(types, values))


class Decoder:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise ValueError(f"truncated value at offset {self.offset}")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def count(self, base: int) -> int:
        marker = self.take(1)[0]
        mask = marker ^ base
        if marker & 0xF0 != base:
            raise ValueError(
                f"invalid collection marker 0x{marker:02x} at offset {self.offset - 1}"
            )
        raw = bytearray(4)
        for index in range(4):
            if mask & (1 << index):
                raw[index] = self.take(1)[0]
        return int.from_bytes(raw, "little")

    def integer(self) -> int:
        marker_offset = self.offset
        marker = self.take(1)[0]
        if marker & 0xF0 != 0x50:
            raise ValueError(f"invalid integer marker 0x{marker:02x} at offset {marker_offset}")
        mask = marker & 0x0F
        raw = bytearray(4)
        for index in range(4):
            if mask & (1 << index):
                raw[index] = self.take(1)[0]
        if marker == 0x5F and raw == b"\xff\xff\xff\xff":
            return -1
        return int.from_bytes(raw, "little")

    def value(self, type_name: str):
        start = self.offset
        try:
            return self._value(type_name)
        except ValueError as exc:
            raise ValueError(f"{type_name} at offset {start}: {exc}") from exc

    def _value(self, type_name: str):
        if type_name == "int":
            return self.integer()
        if type_name == "long":
            marker = self.take(1)[0]
            if marker != 0x90:
                raise ValueError(f"invalid long marker 0x{marker:02x} at offset {self.offset - 1}")
            mask = self.take(1)[0]
            raw = bytearray(8)
            for index in range(8):
                if mask & (1 << index):
                    raw[index] = self.take(1)[0]
            return int.from_bytes(raw, "little", signed=bool(mask & 0x80 and raw[7] & 0x80))
        if type_name == "bool":
            raw = self.take(1)[0]
            if raw not in (0, 1):
                raise ValueError(f"invalid bool 0x{raw:02x} at offset {self.offset - 1}")
            return bool(raw)
        if type_name == "string":
            return self.take(self.count(0xA0)).decode("utf-8")
        if type_name == "float":
            marker = self.take(1)[0]
            if marker != 0x70:
                raise ValueError(f"invalid float marker 0x{marker:02x} at offset {self.offset - 1}")
            mask = self.take(1)[0]
            raw = bytearray(4)
            for index in range(4):
                if mask & (1 << index):
                    raw[index] = self.take(1)[0]
            return struct.unpack("<f", raw)[0]
        if type_name == "double":
            marker = self.take(1)[0]
            if marker != 0x80:
                raise ValueError(f"invalid double marker 0x{marker:02x} at offset {self.offset - 1}")
            mask = self.take(1)[0]
            raw = bytearray(8)
            for index in range(8):
                if mask & (1 << index):
                    raw[index] = self.take(1)[0]
            return struct.unpack("<d", raw)[0]

        generic = split_generic(type_name)
        if generic:
            outer, inner = generic
            if outer == "list":
                return [self.value(inner) for _ in range(self.count(0xD0))]
            if outer == "map":
                key_type, value_type = inner.split("|", 1)
                return {
                    self.value(key_type): self.value(value_type)
                    for _ in range(self.count(0xC0))
                }

        if type_name in POD_TYPES:
            field_types = POD_TYPES[type_name]
            by_serial = {serial: name for name, serial in POD_SERIAL_IDS[type_name].items()}
            result = {}
            for _ in range(self.count(0xC0)):
                serial = self.integer()
                name = by_serial.get(serial)
                if name is None:
                    raise ValueError(f"unknown {type_name} field {serial} at offset {self.offset}")
                result[name] = self.value(field_types[name])
            return result
        raise ValueError(f"unsupported protocol type: {type_name}")


def decode_method(message_id: int, body: bytes):
    method = METHODS.get(message_id)
    if method is None:
        raise KeyError(f"unknown message id {message_id}")
    decoder = Decoder(body)
    values = [decoder.value(type_name) for type_name in method["types"]]
    if decoder.offset != len(body):
        raise ValueError(f"trailing bytes at offset {decoder.offset}")
    return values
