
from __future__ import annotations

import hashlib
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

P25519 = (1 << 255) - 19
_A24 = 121665

CYGAMES_PUBKEY = bytes.fromhex("5aebf09dcc92bbb819c891356a114a3e98e62cc7fa9e1fe0a3cd06537ea5f388")


def x25519_ladder(scalar: int, u: int) -> int:
    p = P25519
    x1 = u % p
    x2, z2, x3, z3 = 1, 0, u % p, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (scalar >> t) & 1
        swap ^= kt
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = kt
        a = (x2 + z2) % p
        aa = a * a % p
        b = (x2 - z2) % p
        bb = b * b % p
        e = (aa - bb) % p
        c = (x3 + z3) % p
        d = (x3 - z3) % p
        da = d * a % p
        cb = c * b % p
        x3 = (da + cb) % p
        x3 = x3 * x3 % p
        z3 = (da - cb) % p
        z3 = z3 * z3 % p * x1 % p
        x2 = aa * bb % p
        z2 = e * ((aa + _A24 * e) % p) % p
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return x2 * pow(z2, p - 2, p) % p


_PCG_MULT = 0x5851F42D4C957F2D
_U64 = (1 << 64) - 1


def _pcg_keystream(env: bytes, n: int) -> bytes:
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
    k = int.from_bytes(_pcg_keystream(env, 32), "big") >> 1
    k |= 1 << 254
    k &= ~0b111
    return k


def ec_export_and_secret(env: bytes, ec_u: bytes) -> tuple[bytes, bytes]:
    k = ephemeral_scalar(env)
    u_srv = int.from_bytes(ec_u, "big") % P25519
    return (x25519_ladder(k, 9).to_bytes(32, "little"),
            x25519_ladder(k, u_srv).to_bytes(32, "little"))


def derive_key(sbox: bytes, sid: bytes, tail16: bytes) -> bytes:
    return hashlib.md5(sbox + sid + tail16).digest()


def spn_whiten(sbox32: bytes, md5hdr: bytes, keyfb16: bytes) -> bytes:
    state = bytearray(sbox32[0:16])
    for p in range(16):
        c = md5hdr[p]
        a = c ^ state[p]
        n1 = a & 0xF
        b = (c ^ sbox32[n1]) & 0xFF
        n2 = b & 0xF
        old = state[n2]
        state[n2] = (old ^ a) & 0xFF
        cc = (old ^ c) & 0xFF
        state[cc & 0xF] = (state[cc & 0xF] ^ b) & 0xFF
        c = (c ^ keyfb16[cc & 0xF]) & 0xFF
        state[c & 0xF] = (state[c & 0xF] ^ cc) & 0xFF
        state[n1] = (state[n1] ^ c) & 0xFF
    return bytes(state)


def gcm_seal(key: bytes, iv: bytes, pt: bytes, aad: bytes) -> tuple[bytes, bytes]:
    out = AESGCM(key).encrypt(iv, pt, aad)
    return out[:-16], out[-16:]


def gcm_open(key: bytes, iv: bytes, ct: bytes, tag: bytes, aad: bytes) -> bytes | None:
    try:
        return AESGCM(key).decrypt(iv, ct + tag, aad)
    except InvalidTag:
        return None


def ctr_crypt(sbox: bytes, ec_export: bytes, data: bytes) -> bytes:
    return Cipher(algorithms.AES(sbox), modes.CTR(ec_export[0:16])).encryptor().update(data)


def seal_0a6(env: bytes, ec_u: bytes) -> bytes:
    env = bytes(env)
    ec_export, sbox = ec_export_and_secret(env, ec_u)
    md5hdr = hashlib.md5(env[0x38:0x48] + env[0x9A:0xAA] + env[0x48:0x58]).digest()
    ctr_h = ctr_crypt(sbox, ec_export, b"\x01\x00" + env[0x78:0x9A])
    key = hashlib.md5(spn_whiten(sbox, md5hdr, env[0x24:0x34])
                      + sbox + md5hdr + env[0x24:0x38]).digest()
    iv = hashlib.md5(ec_export + env[0x38:0x48] + env[0x48:0x58]).digest()
    ct, tag = gcm_seal(key, iv, env[0xAA:], env[0x24:0x58])
    return struct.pack("<I", len(ct) + 0x58 - 4) + ec_export + ctr_h + tag + ct


def seal_074(env: bytes, ec_u: bytes) -> bytes:
    env = bytes(env)
    ec_export, sbox = ec_export_and_secret(env, ec_u)
    udid = env[0x48:0x58]
    ctr_h = ctr_crypt(sbox, ec_export, b"\x00\x00" + udid + _pcg_keystream(env, 130)[96:114])
    key = hashlib.md5(spn_whiten(sbox, udid, env[0x24:0x34])
                      + sbox + udid + env[0x24:0x38]).digest()
    iv = hashlib.md5(ec_export + env[0x38:0x48] + udid).digest()
    ct, tag = gcm_seal(key, iv, env[0x78:], env[0x24:0x58])
    return struct.pack("<I", len(ct) + 0x58 - 4) + ec_export + ctr_h + tag + ct
