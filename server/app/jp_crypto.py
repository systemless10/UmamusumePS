"""
JP transport crypto -- entirely separate from crypto.py (Global), because the
underlying algorithms are fundamentally different, not just parameterized
differently: Global is symmetric with the key traveling in the clear (any
receiver can read it); JP is asymmetric, sealed to a server-held X25519
public key with a per-request ephemeral secret.

Ported from docs/here/codec/coneshell_codec.py (X25519 ladder, ephemeral-key
derivation, AES-128-GCM primitives -- all independently verified there: KDF
self-test, cold-X25519 vector, and the hand-rolled GCM cross-checked byte-for-
byte against the `cryptography` library). NOT ported: that module's own
`reply_key`/`open_wire` -- those still implement an older, since-corrected
theory of the reply key (a cold-constant xser rather than the per-request
sbox). This module's `seal_reply` instead follows the corrected formula from
docs/here/HANDOFF_JP_CRYPTO.md #2: key = MD5(sbox || SID || env_tail16).

We only ever need the REPLY direction here (seal a response so the real
client's own decrypt succeeds) -- see jp_bridge.py's module docstring for why
the request direction never needs decrypting at all.
"""

from __future__ import annotations

import hashlib
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# -- Curve25519 field (2^255 - 19) ---------------------------------------------------------------
P25519 = (1 << 255) - 19
A24 = 121665

EC_U_DEFAULT = bytes.fromhex(
    "5aebf09dcc92bbb819c891356a114a3e98e62cc7fa9e1fe0a3cd06537ea5f388"
)

_PCG_MULT = 0x5851F42D4C957F2D
_U64 = (1 << 64) - 1
_R = 0xE1 << 120  # GHASH reduction polynomial (bit-reflected)


def x25519_ladder(scalar: int, u: int) -> int:
    """Montgomery ladder (RFC 7748). Returns the resulting u-coordinate."""
    p = P25519
    x1 = u % p
    x2, z2 = 1, 0
    x3, z3 = u % p, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (scalar >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % p
        aa = (a * a) % p
        b = (x2 - z2) % p
        bb = (b * b) % p
        e = (aa - bb) % p
        c = (x3 + z3) % p
        d = (x3 - z3) % p
        da = (d * a) % p
        cb = (c * b) % p
        x3 = (da + cb) % p
        x3 = (x3 * x3) % p
        z3 = (da - cb) % p
        z3 = (z3 * z3) % p
        z3 = (z3 * x1) % p
        x2 = (aa * bb) % p
        z2 = (e * ((aa + A24 * e) % p)) % p
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, p - 2, p)) % p


def _pcg_keystream(env: bytes, n: int) -> bytes:
    """PCG64-XSH-RR byte generator seeded from the envelope's seed field."""
    inc = ((int.from_bytes(env[0x60:0x68], "big") << 1) | 1) & _U64
    st = (int.from_bytes(env[0x58:0x60], "big") + inc) & _U64
    st = (st * _PCG_MULT + inc) & _U64
    for _ in range(env[0x38] & 0xF):
        st = (st * _PCG_MULT + inc) & _U64
    out = bytearray()
    for _ in range(n):
        old = st
        st = (old * _PCG_MULT + inc) & _U64
        xsh = ((old >> 0x2D) ^ (old >> 0x1B)) & 0xFFFFFFFF
        rot = (old >> 0x3B) & 0x1F
        lo = xsh & 0xFF
        out.append((((lo << ((-rot) & 0x1F)) & 0xFF) | ((xsh >> rot) & 0xFF)) & 0xFF)
    return bytes(out)


def ephemeral_scalar(env: bytes) -> int:
    """The clamped Curve25519 private scalar the client derived for this request."""
    raw = int.from_bytes(_pcg_keystream(env, 32), "big")
    k = raw >> 1
    k |= 1 << 254
    k &= ~0b111
    return k


def ec_export_and_secret(env: bytes, ec_u: bytes | None = None) -> tuple[bytes, bytes]:
    """(ec_export, sbox): the client's ephemeral pubkey and the ECDH shared secret.
    `ec_u` = the server's static X25519 pubkey; pass the LIVE value from the
    envelope itself (env[4:36]) rather than the default, in case it rotates."""
    k = ephemeral_scalar(env)
    if ec_u is None:
        ec_u = EC_U_DEFAULT
    u_srv = int.from_bytes(ec_u, "big") % P25519
    ec_export = x25519_ladder(k, 9).to_bytes(32, "little")
    sbox = x25519_ladder(k, u_srv).to_bytes(32, "little")
    return ec_export, sbox


# -- AES-128-GCM (hand-rolled, cross-checked against `cryptography`'s AESGCM) --------------------
def _aes_ecb(key: bytes, block16: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block16) + enc.finalize()


def _gf_mul(x: int, y: int) -> int:
    z = 0
    v = x
    for i in range(128):
        if (y >> (127 - i)) & 1:
            z ^= v
        v = (v >> 1) ^ _R if (v & 1) else (v >> 1)
    return z


def _ghash(h: int, data: bytes, y: int = 0) -> int:
    for i in range(0, len(data), 16):
        b = data[i:i + 16]
        b = b + b"\x00" * (16 - len(b))
        y = _gf_mul(y ^ int.from_bytes(b, "big"), h)
    return y


def _j0(h: int, iv: bytes) -> bytes:
    if len(iv) == 12:
        return iv + b"\x00\x00\x00\x01"
    pad = (16 - len(iv) % 16) % 16
    data = iv + b"\x00" * pad + struct.pack(">QQ", 0, len(iv) * 8)
    return _ghash(h, data).to_bytes(16, "big")


def _inc32(v: int) -> int:
    return (v & ~0xFFFFFFFF) | ((v + 1) & 0xFFFFFFFF)


def gcm_crypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-GCM keystream XOR -- encrypt and decrypt are the same operation."""
    h = int.from_bytes(_aes_ecb(key, b"\x00" * 16), "big")
    ctr = int.from_bytes(_j0(h, iv), "big")
    out = bytearray(len(data))
    for i in range(0, len(data), 16):
        ctr = _inc32(ctr)
        ks = _aes_ecb(key, ctr.to_bytes(16, "big"))
        blk = data[i:i + 16]
        out[i:i + len(blk)] = bytes(a ^ b for a, b in zip(blk, ks))
    return bytes(out)


def gcm_tag(key: bytes, iv: bytes, ct: bytes, aad: bytes) -> bytes:
    h = int.from_bytes(_aes_ecb(key, b"\x00" * 16), "big")
    j0 = _j0(h, iv)
    y = _ghash(h, aad)
    y = _ghash(h, ct, y)
    y = _gf_mul(y ^ int.from_bytes(struct.pack(">QQ", len(aad) * 8, len(ct) * 8), "big"), h)
    ej0 = int.from_bytes(_aes_ecb(key, j0), "big")
    return (y ^ ej0).to_bytes(16, "big")


# -- Reply sealing (server -> client) -------------------------------------------------------------
# Formula per docs/here/HANDOFF_JP_CRYPTO.md #2 (the corrected theory, NOT
# coneshell_codec.py's own reply_key/open_wire):
#   key = MD5(sbox || SID || env_tail16)
#   wire = LE32(out_size) || iv(16) || tag(16) || ct
# out_size = 0 means "uncompressed" -- the client reads the body directly,
# skipping LZ4 entirely. Always used here: AES-GCM has no block-size
# constraint, so there's no correctness reason to compress, only bandwidth
# (irrelevant on localhost).
def seal_reply(msgpack_body: bytes, sbox: bytes, sid: bytes, env_tail16: bytes) -> bytes:
    key = hashlib.md5(sbox + sid + env_tail16).digest()
    iv = os.urandom(16)
    ct = gcm_crypt(key, iv, msgpack_body)
    tag = gcm_tag(key, iv, ct, b"")
    return struct.pack("<I", 0) + iv + tag + ct
