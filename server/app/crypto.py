"""
Real wire protocol for the Umamusume Steam client, reverse-engineered in
../../../Icarus-Dev-Build-Private-main/uma_api/client.py (pack()/unpack()).
Reimplemented here (not imported) to keep this server standalone from that
project.

Request (client -> server), POST body as base64 ASCII text:

    base64( u32_LE(len(header)) + header + body )

    header = HEAD (52-byte fixed magic) + sid (16B) + udid_raw (16B)
             + random (32B) + auth (variable, may be empty)
    body   = AES-CBC(key, iv)(pad(u32_LE(len(msgpack_payload)) + msgpack_payload))
             + key (32B, appended in the clear)
    iv     = first 16 hex chars of udid_raw.hex(), as ASCII bytes

Response (server -> client), HTTP body as base64 ASCII text:

    base64( body )   # same body construction, no header

The AES key is generated fresh per message and travels appended to the
ciphertext (not pre-shared) -- both directions are self-decrypting given
just the udid, which the server reads straight out of the request header.
This means the server needs no secret material at all to talk this
protocol, only to parse it correctly.
"""

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
    auth = header[_HEADER_FIXED_LEN:]  # anything after HEAD+sid+udid_raw+random32

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
