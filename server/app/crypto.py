
from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass

import msgpack
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

HEAD = bytes.fromhex(
    "6b20e2ab6c311330f761d737ce3f3025750850665eea58b6372f8d2f57501eb"
    "344bdb7270a9067f5b63cd61f152cfb986cbfbf7a"
)
_SID_LEN = 16
_UDID_RAW_LEN = 16
_RANDOM_LEN = 32
_HEADER_FIXED_LEN = len(HEAD) + _SID_LEN + _UDID_RAW_LEN + _RANDOM_LEN


@dataclass
class DecodedRequest:
    payload: dict
    sid: bytes
    udid_raw: bytes
    auth: bytes


def _iv_for(udid_raw: bytes) -> bytes:
    return udid_raw.hex()[:16].encode()


def decode_request(raw: bytes, headers: dict) -> DecodedRequest:
    blob = base64.b64decode(raw)
    (header_len,) = struct.unpack("<I", blob[:4])
    header = blob[4 : 4 + header_len]
    body = blob[4 + header_len :]

    if header[: len(HEAD)] != HEAD:
        raise ValueError("request header does not start with the expected magic")

    sid = header[len(HEAD) : len(HEAD) + _SID_LEN]
    udid_raw = header[
        len(HEAD) + _SID_LEN : len(HEAD) + _SID_LEN + _UDID_RAW_LEN
    ]
    auth = header[_HEADER_FIXED_LEN:]

    key, cipher = body[-32:], body[:-32]
    plain = unpad(AES.new(key, AES.MODE_CBC, _iv_for(udid_raw)).decrypt(cipher), 16)
    (payload_len,) = struct.unpack("<I", plain[:4])
    payload = msgpack.unpackb(
        plain[4 : 4 + payload_len], raw=False, strict_map_key=False
    )
    return DecodedRequest(payload=payload, sid=sid, udid_raw=udid_raw, auth=auth)


def encode_response(payload: dict, udid_raw: bytes) -> bytes:
    key = os.urandom(32)
    packed = msgpack.packb(payload, use_bin_type=True)
    ciphertext = AES.new(key, AES.MODE_CBC, _iv_for(udid_raw)).encrypt(
        pad(struct.pack("<I", len(packed)) + packed, 16)
    )
    return base64.b64encode(ciphertext + key)
