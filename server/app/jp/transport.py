
from __future__ import annotations

import base64
import gzip
import hashlib
import os
import struct
from dataclasses import dataclass, field
from typing import Iterable, Protocol

import lz4.block
import msgpack

from . import coneshell as cs

JP_SALT = b"r!I@mt8e5i="
SESSION, BOUNDARY = 0xA6, 0x74

KNOWN_TAIL20 = (
    bytes.fromhex("fed5204454857f24ba09fad010e2c988c5684b90"),
    bytes.fromhex("964f11a0cc8024c558877ef091bedc8337df0a6b"),
    bytes.fromhex("84ab20d67f3ca2b3f72a573718f21e341739e2ec"),
    bytes.fromhex("4f5aeedd2b55b29f6a072b59fc01ed2604e17d5d"),
)

COMPRESS_OVER = 512


class WrongKey(Exception):
    pass


class BadRequest(Exception):
    pass


@dataclass
class Session:
    udid: bytes
    tail50: bytes

    @property
    def tail16(self) -> bytes:
        return self.tail50[34:50]


class SessionSource(Protocol):
    def session_for(self, viewer_id: str) -> Session | None: ...
    def tail20_candidates(self) -> Iterable[bytes]: ...


@dataclass
class DecodedRequest:
    payload: dict
    sid: bytes
    udid: bytes
    kind: int
    sbox: bytes
    tail16: bytes | None = None
    tail20: bytes = field(default=b"", repr=False)
    tail50: bytes | None = field(default=None, repr=False)


def make_sid(viewer_id, udid_str: str) -> bytes:
    return hashlib.md5((str(viewer_id) + udid_str).encode() + JP_SALT).digest()


def next_sid(resp_sid: str) -> bytes:
    return hashlib.md5(resp_sid.encode() + JP_SALT).digest()


def open_request(raw: bytes, headers: dict, priv_scalar: int,
                 sessions: SessionSource) -> DecodedRequest:
    try:
        wire = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise BadRequest(f"body is not base64: {exc}") from exc
    if len(wire) < 0x58:
        raise BadRequest(f"wire too short ({len(wire)} B)")

    ec_export, ctr_header, tag, ct = wire[0x04:0x24], wire[0x24:0x48], wire[0x48:0x58], wire[0x58:]
    declared = struct.unpack_from("<I", wire, 0)[0]
    if declared != len(ct) + 0x58 - 4:
        raise BadRequest(f"length field {declared} != {len(ct) + 0x58 - 4}")

    sbox = cs.x25519_ladder(priv_scalar, int.from_bytes(ec_export, "little")).to_bytes(32, "little")
    ctr_pt = cs.ctr_crypt(sbox, ec_export, ctr_header)
    kind = {0x01: SESSION, 0x00: BOUNDARY}.get(ctr_pt[0])
    if kind is None or ctr_pt[1] != 0x00:
        raise WrongKey(f"ctr_header opened to {ctr_pt[:2].hex()} -- the client did not seal to our key")

    h = {k.lower(): v for k, v in headers.items()}
    try:
        sid = bytes.fromhex(h.get("sid") or "")
    except ValueError as exc:
        raise BadRequest("SID header is not hex") from exc
    if len(sid) != 16:
        raise BadRequest("request has no 16-byte SID header")

    if kind == BOUNDARY:
        udid, tail16 = ctr_pt[2:18], None
        md5hdr = udid
    else:
        viewer = str(h.get("viewerid") or "0")
        sess = sessions.session_for(viewer)
        if sess is not None and ctr_pt[2:36] != sess.tail50[0:34]:
            for alt in getattr(sessions, "reported_sessions", lambda: [])():
                if ctr_pt[2:36] == alt.tail50[0:34]:
                    sess = alt
                    break
        if sess is None:
            raise BadRequest(f"0xA6 request for viewer {viewer} with no known session "
                             "(no signup here, and the plugin has not reported one)")
        if ctr_pt[2:36] != sess.tail50[0:34]:
            reported = getattr(sessions, "reported_session", lambda: None)()
            if reported is None or ctr_pt[2:36] != reported.tail50[0:34]:
                raise BadRequest(f"tail50 mismatch for viewer {viewer}")
            sess = reported
        udid, tail16 = sess.udid, sess.tail16
        md5hdr = hashlib.md5(sid + tail16 + udid).digest()

    iv = hashlib.md5(ec_export + sid + udid).digest()
    seen: set[bytes] = set()
    for tail20 in (*sessions.tail20_candidates(), *KNOWN_TAIL20):
        if tail20 in seen:
            continue
        seen.add(tail20)
        key = hashlib.md5(cs.spn_whiten(sbox, md5hdr, tail20[0:16]) + sbox + md5hdr + tail20).digest()
        body = cs.gcm_open(key, iv, ct, tag, tail20 + sid + udid)
        if body is not None:
            payload = msgpack.unpackb(body, raw=False, strict_map_key=False)
            return DecodedRequest(payload=payload, sid=sid, udid=udid, kind=kind,
                                  sbox=sbox, tail16=tail16, tail20=tail20,
                                  tail50=sess.tail50 if kind == SESSION else None)
    raise BadRequest("GCM tag mismatch under every known build tail "
                     "(new client build? the plugin reports the live tail20)")


def seal_reply(payload: dict, decoded: DecodedRequest) -> bytes:
    body = msgpack.packb(payload, use_bin_type=True)
    if len(body) > COMPRESS_OVER:
        pt, out_size = lz4.block.compress(body, store_size=False), len(body)
    else:
        pt, out_size = body, 0
    tail = decoded.udid if decoded.kind == BOUNDARY else decoded.tail16
    key = cs.derive_key(decoded.sbox, decoded.sid, tail)
    iv = os.urandom(16)
    ct, tag = cs.gcm_seal(key, iv, pt, decoded.sid + tail)
    return struct.pack("<I", out_size) + iv + tag + ct


def encode_http_body(wire: bytes, accept_encoding: str) -> tuple[bytes, dict]:
    out = base64.b64encode(wire)
    if "gzip" in (accept_encoding or "").lower():
        return gzip.compress(out), {"Content-Encoding": "gzip"}
    return out, {}
