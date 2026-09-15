#!/usr/bin/env python3
"""
TLS capture proxy for the Umamusume client -- the Linux replacement for UmaDumpy.

WHY THIS EXISTS
---------------
UmaDumpy captures by injecting Frida into the game. That is impossible under
Proton: Frida's bootstrapper segfaults injecting into a Wine process, and even
past that its Linux backend cannot parse the PE modules Wine maps, so the
winhttp.dll / GameAssembly.dll hooks can never resolve. See the
"Linux / Proton" section of UmaDumpy's README.

This takes the other route. The client already trusts our self-signed cert (it
is in the Proton prefix's CurrentUser\\ROOT store) and does no certificate
pinning, so we can simply sit in the middle: terminate TLS, record the exchange,
forward it on, record the reply.

TWO MODES
---------
  --upstream real   forward to the REAL Cygames server (resolved past /etc/hosts).
                    This is how you capture GROUND TRUTH to diff our responses
                    against.
  --upstream local  forward to our own private server on another port. This is
                    how you record exactly what WE send, decoded.

TWO REGIONS
-----------
  --region global   (default) api.games.umamusume.com -- the Steam client.
  --region jp       api.games.umamusume.jp -- the JP (DMM/mobile) client.
                    Uses its own self-signed cert (server.cert.jp.pem /
                    server.key.jp.pem, auto-generated on first use next to
                    the existing global cert). That new cert is NOT trusted
                    by anything yet -- install it into whatever trust store
                    the JP client checks (same idea as the existing cert's
                    install step) or every JP capture will just be a TLS
                    handshake failure. Output filenames get a "jp_" tag so
                    they're never confused with global captures.

REQUIREMENTS
------------
* The /etc/hosts redirect must be ON for whichever domain you're capturing
  (see toggle-redirect.ps1 -Domain global|jp -- the client must reach us,
  not Cygames).
* Port 443 must be free -- stop the private server first, or run it on another
  port and use --upstream local.
* Run with the capability-granted interpreter so port 443 binds without root:
      server/.venv/bin/python3.14 tools/capture_proxy.py ...

IMPORTANT for --upstream real
-----------------------------
The account in the client's SaveData.db must be one the REAL server knows. The
viewer_id currently in the Linux save was minted by OUR tool/signup and does not
exist at Cygames, so a real-mode capture with it will just record rejections.
Restore a real account's save first (saved_data_backups/) and put it back after.

OUTPUT
------
captures/<session>/NNNN_<endpoint>.json -- one file per transaction, with the
decoded msgpack (best effort, so a decode bug can never lose the capture).
Before writing, each record is run through capture_redact.redact_record():
auth tokens/session ids/device fingerprint/IP are stripped, the still-
encrypted raw request/response bytes are dropped outright, and viewer ids are
replaced with a stable per-account synthetic id. See capture_redact.py for
the full list and the reasoning.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import http.client
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "server"))

import msgpack  # noqa: E402
from Crypto.Cipher import AES  # noqa: E402
from Crypto.Util import Counter as _CryptoCounter  # noqa: E402
from Crypto.Util.Padding import unpad  # noqa: E402
from app import crypto  # noqa: E402  -- reuse the server's own wire protocol
from capture_redact import IdMapper, redact_record  # noqa: E402

CERT = os.path.join(_ROOT, "server", "certs", "server.cert.pem")
KEY = os.path.join(_ROOT, "server", "certs", "server.key.pem")

# One real Cygames domain per client build. "global" is the Steam client this
# proxy originally targeted; "jp" is the DMM/mobile client (host confirmed via
# Documents/game-dumps/JPdump.cs's APPLICATION_SERVER_URL constant). Each gets
# its own cert/key pair since a cert's SAN is bound to one hostname.
REGIONS = {
    "global": {
        "domain": "api.games.umamusume.com",
        "cert": CERT,
        "key": KEY,
    },
    "jp": {
        "domain": "api.games.umamusume.jp",
        "cert": os.path.join(_ROOT, "server", "certs", "server.cert.jp.pem"),
        "key": os.path.join(_ROOT, "server", "certs", "server.key.jp.pem"),
    },
}

_seq_lock = threading.Lock()
_seq = 0
SESSION_DIR = ""
_id_mapper = IdMapper()


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def resolve_real_ip(domain: str) -> str:
    """Resolve `domain` past /etc/hosts (which points it at us): a minimal raw
    DNS A-record query straight to 1.1.1.1 over UDP (port 53), stdlib-only,
    no external process and no TLS/cert dependency.

    Originally shelled out to `dig +short ... @1.1.1.1` (Linux-only -- `dig`
    isn't on Windows by default). A DNS-over-HTTPS JSON lookup was tried as a
    replacement but failed with SSL_CERTIFICATE_VERIFY_FAILED on this Windows
    Python install (no local CA bundle configured); plain UDP has no such
    dependency and stays true to the original "ask 1.1.1.1 directly" intent."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\x00"
    header = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    query = header + qname + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    resp = None
    last_err = None
    # Try more than one public resolver: a single hardcoded one is a needless
    # single point of failure on a network that blocks or hijacks it.
    for server in ("1.1.1.1", "8.8.8.8", "9.9.9.9"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(5)
                sock.sendto(query, (server, 53))
                resp, _ = sock.recvfrom(4096)
            break
        except OSError as e:
            last_err = e
    if resp is None:
        raise RuntimeError(f"could not resolve {domain}; is DNS reachable? {last_err}")

    (ancount,) = struct.unpack(">H", resp[6:8])
    pos = len(header) + len(qname) + 4     # past the echoed question section
    for _ in range(ancount):
        # A record's NAME is either a 2-byte compression pointer (0xC0..) or a
        # literal length-prefixed name. Assuming the pointer form unconditionally
        # desynchronises the walk the moment a resolver sends a literal one --
        # this domain answers with a CNAME + A pair, and the mis-stepped offset
        # then ran past the end of the packet ("unpack requires a buffer of 10
        # bytes", which is how this surfaced).
        if pos >= len(resp):
            break
        if resp[pos] & 0xC0 == 0xC0:
            pos += 2
        else:
            while pos < len(resp) and resp[pos]:
                pos += resp[pos] + 1
            pos += 1
        if pos + 10 > len(resp):
            break
        rtype, _, _, rdlen = struct.unpack(">HHIH", resp[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:       # A record
            return ".".join(str(b) for b in resp[pos:pos + 4])
        pos += rdlen
    raise RuntimeError(f"could not resolve {domain}; is DNS reachable?")


def ensure_region_cert(cert_path: str, key_path: str, domain: str) -> None:
    """Generate a self-signed cert/key for `domain` if one isn't already there.

    Never touches the global cert (that one predates this script and is
    presumably already installed as a trusted root somewhere); only used for
    a non-global region's cert, which nothing trusts yet on first run."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", key_path, "-out", cert_path,
         "-days", "3650", "-nodes",
         "-subj", f"/CN={domain}",
         "-addext", f"subjectAltName=DNS:{domain}",
         "-addext", "basicConstraints=critical,CA:TRUE"],
        check=True,
    )
    print(f"generated new self-signed cert for {domain} at {cert_path}\n"
          f"  NOTE: nothing trusts this cert yet. Install it the same way the\n"
          f"  existing server/certs/server.cert.pem was installed for the\n"
          f"  global client, or captures will just record TLS handshake\n"
          f"  failures instead of real traffic.")


# Candidate static key found in Documents/game-dumps/JPdump.cs's AES256Crypt
# class (a literal `KEYSTR` constant, 32 bytes -- AES-256 key size). The JP
# client build appears to use a fixed shared key rather than global's
# per-message ephemeral key, per that class's Encrypt/Decrypt(string, iv)
# signature (a key that traveled with the message wouldn't need one).
_JP_CANDIDATE_KEY = "s%5VNQ(H$&Bqb6#3+78h29!Ft4wSg)ex".encode()


def _try_msgpack(plain: bytes):
    """Try both 'raw msgpack' and 'u32_LE(len) + msgpack' (the global
    protocol's inner framing) against decrypted plaintext."""
    try:
        return ("raw", msgpack.unpackb(plain, raw=False, strict_map_key=False))
    except Exception:
        pass
    if len(plain) >= 4:
        try:
            (n,) = struct.unpack("<I", plain[:4])
            if 0 < n <= len(plain) - 4:
                return ("len-prefixed", msgpack.unpackb(plain[4:4 + n], raw=False, strict_map_key=False))
        except Exception:
            pass
    return None


def _diagnose_jp_crypto(candidate: bytes) -> None:
    """Oracle-test the JP client's likely wire format against real ciphertext
    already in memory.

    Working hypothesis (per user input): unlike global (cleartext header +
    encrypted body), JP encrypts header-and-body TOGETHER as one blob, with
    the same field family reordered: common header (magic) + sid(16) +
    udid(16) + auth key + random(32), then presumably the same
    u32_LE(len)+msgpack body convention afterward. That explains why no fixed
    magic survives on the wire (it's inside the ciphertext) and why lengths
    aren't block-aligned (stream mode, not padded CBC).

    Oracle: decrypt, then check plaintext.startswith(crypto.HEAD) -- the
    52-byte magic is a known exact constant, so a match is essentially
    unambiguous, unlike hoping a byte-misaligned msgpack parse happens to
    succeed. Console-only, nothing persisted."""
    key_candidates = [("KEYSTR", _JP_CANDIDATE_KEY)]
    if len(candidate) > 32:
        key_candidates.append(("trailing32", candidate[-32:]))

    for key_name, key in key_candidates:
        cipher_base = candidate[:-32] if key_name == "trailing32" else candidate
        iv_variants = [("zero", b"\x00" * 16, cipher_base)]
        if len(cipher_base) > 16:
            iv_variants.append(("first16", cipher_base[:16], cipher_base[16:]))
            iv_variants.append(("last16", cipher_base[-16:], cipher_base[:-16]))

        for iv_name, iv, cipher in iv_variants:
            for mode_name in ("CFB", "OFB", "CTR"):
                try:
                    if mode_name == "CFB":
                        plain = AES.new(key, AES.MODE_CFB, iv, segment_size=128).decrypt(cipher)
                    elif mode_name == "OFB":
                        plain = AES.new(key, AES.MODE_OFB, iv).decrypt(cipher)
                    else:
                        ctr = _CryptoCounter.new(128, initial_value=int.from_bytes(iv, "big"))
                        plain = AES.new(key, AES.MODE_CTR, counter=ctr).decrypt(cipher)
                except Exception:
                    continue
                if plain.startswith(crypto.HEAD):
                    print(f"  [diag-jp] *** HEAD MATCH *** key={key_name} iv={iv_name} mode={mode_name}")
                    print(f"  [diag-jp] plaintext[:200]: {plain[:200].hex()}")
                    hit = _try_msgpack(plain[116:])
                    if hit is not None:
                        framing, payload = hit
                        keys = sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
                        print(f"  [diag-jp] body decoded ({framing}) at offset 116, keys: {keys}")
                    else:
                        print("  [diag-jp] body didn't parse at offset 116 -- auth field may be non-empty here")
                    return
    print(f"  [diag-jp] no HEAD match across {len(key_candidates)} keys x "
          f"{{zero,first16,last16}} iv x {{CFB,OFB,CTR}} modes")


def _diagnose_magic_mismatch(req_body: bytes) -> None:
    """Best-effort, console-only: on a magic mismatch, try the SAME header
    layout as the global protocol (52B magic + 16B sid + 16B udid + 32B
    random -- see crypto.py's docstring) against the raw bytes anyway, in
    case only the magic constant differs between client builds and not the
    whole framing. Never touches the persisted capture record -- nothing
    from here reaches disk, so it's fine that this briefly holds the
    cleartext sid/udid in memory."""
    try:
        blob = base64.b64decode(req_body)
        (length_field,) = struct.unpack("<I", blob[:4])
        ciphertext_candidate = blob[4:4 + length_field]
        print(f"  [diag] blob={len(blob)}B length_field={length_field} "
              f"ciphertext_candidate={len(ciphertext_candidate)}B "
              f"(not a multiple of 16 -> stream mode, not padded CBC)")
        _diagnose_jp_crypto(ciphertext_candidate)
    except Exception as exc:
        print(f"  [diag] couldn't even get this far: {exc!r}")


def decode_response(raw_body: bytes, udid_raw: bytes):
    """Inverse of crypto.encode_response (the server module only encodes)."""
    blob = base64.b64decode(raw_body)
    key, cipher = blob[-32:], blob[:-32]
    plain = unpad(AES.new(key, AES.MODE_CBC, crypto._iv_for(udid_raw)).decrypt(cipher), 16)
    (length,) = struct.unpack("<I", plain[:4])
    return msgpack.unpackb(plain[4:4 + length], raw=False, strict_map_key=False)


class UpstreamHTTPS(http.client.HTTPSConnection):
    """HTTPS to a fixed IP while presenting the real hostname for SNI/cert.

    Connecting by hostname would hit our own /etc/hosts entry and loop straight
    back into this proxy, so the address is pinned and only SNI carries the name.
    """

    def __init__(self, ip: str, hostname: str, timeout: int = 30):
        super().__init__(hostname, 443, timeout=timeout,
                         context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream_ip = ""
    upstream_kind = "real"
    upstream_port = 443
    region = "global"
    domain = REGIONS["global"]["domain"]

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req_body = self.rfile.read(length) if length else b""
        endpoint = self.path.strip("/")
        if endpoint.startswith("umamusume/"):
            endpoint = endpoint[len("umamusume/"):]

        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection")}
        fwd_headers["Host"] = self.domain

        try:
            if self.upstream_kind == "real":
                conn = UpstreamHTTPS(self.upstream_ip, self.domain)
            else:
                conn = http.client.HTTPSConnection(
                    "127.0.0.1", self.upstream_port, timeout=30,
                    context=ssl._create_unverified_context())
            conn.request("POST", self.path, body=req_body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            status, resp_headers = resp.status, dict(resp.getheaders())
            conn.close()
        except Exception as exc:  # upstream failure is itself worth recording
            self._record(endpoint, req_body, b"", 0, {}, error=repr(exc))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(f"  !! {endpoint}: upstream error {exc}")
            return

        self._record(endpoint, req_body, resp_body, status, resp_headers)

        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _record(self, endpoint, req_body, resp_body, status, resp_headers, error=None):
        n = _next_seq()
        rec = {
            "seq": n,
            "endpoint": endpoint,
            "path": self.path,
            "status": status,
            "region": self.region,
            "upstream": self.upstream_kind,
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "request_headers": dict(self.headers),
            "response_headers": resp_headers,
            "raw": {
                "request_b64": req_body.decode("ascii", "replace"),
                "response_b64": resp_body.decode("ascii", "replace"),
            },
        }
        if error:
            rec["error"] = error

        udid = None
        try:
            decoded = crypto.decode_request(req_body, dict(self.headers))
            rec["request"] = decoded.payload
            udid = decoded.udid_raw
            rec["udid_hex"] = udid.hex()
        except Exception as exc:
            rec["request_decode_error"] = repr(exc)
            _diagnose_magic_mismatch(req_body)
        if resp_body and udid is not None:
            try:
                rec["response"] = decode_response(resp_body, udid)
            except Exception as exc:
                rec["response_decode_error"] = repr(exc)

        redact_record(rec, _id_mapper)

        safe = endpoint.replace("/", "_") or "root"
        tag = "" if self.region == "global" else f"{self.region}_"
        path = os.path.join(SESSION_DIR, f"{n:04d}_{tag}{safe}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)

        flag = ""
        if "request_decode_error" in rec:
            flag += " [req-decode-failed]"
        if "response_decode_error" in rec:
            flag += " [resp-decode-failed]"
        print(f"  {n:04d}  {status}  {endpoint}"
              f"  req={len(req_body)}B resp={len(resp_body)}B{flag}")

    def do_GET(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    global SESSION_DIR
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", default="real", choices=["real", "local"],
                    help="forward to the real Cygames server, or to our own server")
    ap.add_argument("--region", default="global", choices=list(REGIONS),
                    help="which client build's domain to capture (default: global)")
    ap.add_argument("--local-port", type=int, default=8443,
                    help="port the private server listens on when --upstream local")
    ap.add_argument("--listen-port", type=int, default=443)
    ap.add_argument("--out", default=os.path.join(_ROOT, "captures"))
    args = ap.parse_args()

    region_cfg = REGIONS[args.region]
    Handler.region = args.region
    Handler.domain = region_cfg["domain"]
    if args.region != "global":
        ensure_region_cert(region_cfg["cert"], region_cfg["key"], region_cfg["domain"])

    session = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.region != "global":
        session += f"_{args.region}"
    SESSION_DIR = os.path.join(args.out, session)
    os.makedirs(SESSION_DIR, exist_ok=True)

    Handler.upstream_kind = args.upstream
    Handler.upstream_port = args.local_port
    if args.upstream == "real":
        Handler.upstream_ip = resolve_real_ip(Handler.domain)
        target = f"REAL Cygames server ({args.region}) at {Handler.upstream_ip}"
    else:
        target = f"local private server at 127.0.0.1:{args.local_port}"

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(region_cfg["cert"], region_cfg["key"])
    httpd = ThreadingHTTPServer(("0.0.0.0", args.listen_port), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(f"capture proxy listening on :{args.listen_port} -> {target}")
    print(f"writing to {SESSION_DIR}")
    if args.upstream == "real":
        print("NOTE: the client's account must exist on the REAL server, or you will\n"
              "      only capture rejections. See this file's docstring.")
    print("Ctrl-C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\nstopped. {_seq} transactions in {SESSION_DIR}")


if __name__ == "__main__":
    main()
