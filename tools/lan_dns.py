#!/usr/bin/env python3
"""
LAN DNS responder -- the hosts-file redirect, for devices that have no hosts file.

WHY THIS EXISTS
---------------
toggle-redirect.ps1 works by writing "127.0.0.1 api.games.umamusume.com" into
the Windows hosts file. An iPhone has no equivalent and no way to get one
without a jailbreak, so the lie has to be told one layer further out: this
answers the phone's DNS queries directly.

The obvious-looking alternative does NOT work. iOS's Wi-Fi "HTTP Proxy" setting
speaks CONNECT, and tools/capture_proxy.py is not a CONNECT proxy at all -- it
is an origin server that terminates TLS for one specific hostname (it only
implements do_POST; do_GET returns 405). DNS is the only interception point
that fits what is already built.

WHAT IT DOES
------------
Answers A queries for the redirected domains with this machine's LAN IP, and
forwards everything else verbatim to a real upstream resolver so the phone
keeps working normally while it is pointed here. Queries it cannot parse or
forward are answered SERVFAIL rather than dropped -- a dropped query costs the
client a multi-second timeout, and on a phone that reads as "the whole network
is broken", which is a miserable thing to debug.

USAGE
-----
    server\\.venv\\Scripts\\python.exe tools\\lan_dns.py
    server\\.venv\\Scripts\\python.exe tools\\lan_dns.py --ip 192.168.1.50
    server\\.venv\\Scripts\\python.exe tools\\lan_dns.py --extra-domain foo.example.com

Then on the iPhone: Settings > Wi-Fi > (i) > Configure DNS > Manual, remove
every entry, add this machine's LAN IP.

REQUIREMENTS
------------
* Binding UDP 53 needs admin on Windows. Run the shell as administrator.
* Windows' own "Internet Connection Sharing" service also binds UDP 53 and
  will take the port if it is running; stop it, or this will fail to bind.
* Allow UDP 53 inbound through the firewall for this interpreter, or the
  phone's queries never arrive and it simply hangs -- no error, no log line
  here, nothing. That silence is the expected symptom, not a sign of a bug.
* The phone and this PC must be on the SAME LAN, with the phone able to route
  to this machine's IP. A PC tethered to the phone's Personal Hotspot does not
  satisfy this: the phone is the gateway there, and its own traffic never
  passes through the PC. Use a normal Wi-Fi router both devices join.

The phone must also not be routing DNS around you. Turn OFF iCloud Private
Relay, and make sure no Encrypted DNS (DoH/DoT) configuration profile is
installed -- either silently overrides the manual DNS you just set.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading

# Kept in lockstep with toggle-redirect.ps1's $DomainMap. The JP host is listed
# for completeness only -- a JP client cannot talk to this server whatever DNS
# says, because its requests are sealed to a Cygames-held X25519 private key
# (see docs/HANDOFF_JP_CRYPTO.md). Redirecting it just gets you a dead client.
DEFAULT_DOMAINS = [
    "api.games.umamusume.com",
]

UPSTREAM_RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]

# Apple's captive-portal / connectivity-check hosts. iOS fetches one of these
# right after associating and looks for an exact "Success" page; if it does not
# get it, the network is flagged as having no internet, and iOS then routes app
# traffic to cellular (or, with cellular off, largely stops using the interface).
# That behaviour is fatal here for a non-obvious reason: this setup does not NEED
# upstream internet -- the game only ever talks to a hostname that resolves to
# this very machine -- but iOS abandons the network before the game ever gets to
# ask. Answering the check locally keeps the phone on the Wi-Fi it is already
# correctly attached to. --captive turns this on.
CAPTIVE_DOMAINS = [
    "captive.apple.com",
    "www.apple.com",
    "gsp1.apple.com",
    "www.itools.info",
    "www.ibook.info",
    "www.airport.us",
    "www.thinkdifferent.us",
]

# Byte-exact body iOS expects. It compares content, not just status, so this
# cannot be an approximation or a friendly placeholder page.
CAPTIVE_BODY = b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>\n"

# Names a phone uses to FIND an encrypted resolver, rather than to reach a
# service. Answering these honestly is what lets a client walk around this
# responder entirely: iOS looks up `_dns.resolver.arpa` (DDR -- Discovery of
# Designated Resolvers) and the DoH provider's own hostname, then sends every
# real query over HTTPS to that provider instead of to us. The game's hostname
# then resolves to Cygames for real, and nothing about it is visible here --
# the log shows only the bootstrap lookups, which is exactly how this presents.
#
# Refusing them forces the client back to plain DNS on port 53, which we serve.
# --block-doh turns this on.
DOH_BOOTSTRAP_DOMAINS = [
    "_dns.resolver.arpa",          # DDR discovery
    "dns.google",
    "dns64.dns.google",
    "one.one.one.one",
    "dns.cloudflare.com",
    "mozilla.cloudflare-dns.com",
    "chrome.cloudflare-dns.com",
    "security.cloudflare-dns.com",
    "family.cloudflare-dns.com",
    "dns.quad9.net",
    "dns10.quad9.net",
    "doh.opendns.com",
    "doh.familyshield.opendns.com",
    "dns.adguard.com",
    "dns.nextdns.io",
]

TYPE_A = 1
CLASS_IN = 1


# Windows' Internet Connection Sharing -- which Mobile Hotspot is built on --
# always numbers its own interface from this scope (the ScopeAddress value under
# HKLM\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters).
ICS_HOTSPOT_IP = "192.168.137.1"


def _have_local_address(ip: str) -> bool:
    """Is `ip` actually assigned to an interface on this machine?

    Binding is the test rather than enumerating adapters: bind() fails with
    EADDRNOTAVAIL for an address this host does not hold, which is precisely
    the question, and it needs no third-party module or ipconfig parsing.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((ip, 0))
        return True
    except OSError:
        return False


def detect_lan_ip() -> str:
    """The address to hand out to the phone.

    Prefers the Mobile Hotspot address when one exists. This ordering matters
    and is not a nicety: with the hotspot running, the connect-to-1.1.1.1 probe
    below reports the address that reaches the INTERNET -- the upstream Wi-Fi
    interface -- which is exactly the network the phone is NOT on. Handing that
    address out produces a game that hangs forever with a DNS log here showing
    a perfectly correct-looking answer.

    Otherwise falls back to the connect-a-UDP-socket trick rather than resolving
    the hostname: gethostbyname on a multi-homed Windows box routinely returns a
    VirtualBox, WSL or VPN adapter's address, and handing the phone one of those
    produces a silent connect timeout that looks exactly like a firewall
    problem. No packet is sent -- connect() on UDP only sets the peer, which is
    enough to make the kernel commit to a source address.
    """
    if _have_local_address(ICS_HOTSPOT_IP):
        print(f"detected Mobile Hotspot interface, using {ICS_HOTSPOT_IP}")
        print("  (pass --ip to override if that is not the one the phone is on)")
        return ICS_HOTSPOT_IP

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    finally:
        sock.close()


def parse_question(packet: bytes) -> tuple[str, int, int] | None:
    """Pull (qname, qtype, qclass) out of a query. None if it is malformed.

    Deliberately does not follow compression pointers: a pointer in the
    question section of a query is not a thing real clients emit, and chasing
    one is how a parser gets walked into an infinite loop by a hostile packet.
    """
    if len(packet) < 12:
        return None
    (qdcount,) = struct.unpack(">H", packet[4:6])
    if qdcount < 1:
        return None
    labels = []
    pos = 12
    while True:
        if pos >= len(packet):
            return None
        length = packet[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0:
            return None
        pos += 1
        if pos + length > len(packet):
            return None
        labels.append(packet[pos:pos + length].decode("ascii", "replace"))
        pos += length
    if pos + 4 > len(packet):
        return None
    qtype, qclass = struct.unpack(">HH", packet[pos:pos + 4])
    return ".".join(labels), qtype, qclass


def build_a_response(query: bytes, ip: str, ttl: int = 60) -> bytes:
    """Query echoed back with one A record appended.

    The question section is copied byte-for-byte from the query rather than
    re-encoded, so whatever casing or padding the client used comes back
    identical -- some stacks check.
    """
    (txid,) = struct.unpack(">H", query[:2])
    (orig_flags,) = struct.unpack(">H", query[2:4])
    rd = orig_flags & 0x0100                      # preserve recursion-desired
    flags = 0x8000 | rd | 0x0080                  # QR=1, RA=1, RCODE=0
    header = struct.pack(">HHHHHH", txid, flags, 1, 1, 0, 0)

    question_end = 12
    while query[question_end]:
        question_end += query[question_end] + 1
    question_end += 1 + 4
    question = query[12:question_end]

    answer = (
        b"\xc0\x0c"                               # pointer to the question's name
        + struct.pack(">HHIH", TYPE_A, CLASS_IN, ttl, 4)
        + socket.inet_aton(ip)
    )
    return header + question + answer


def build_rcode_response(query: bytes, rcode: int) -> bytes:
    """Header-only reply carrying an error code. Used instead of dropping a
    query, so the client fails fast rather than sitting on a timeout."""
    if len(query) < 4:
        return b""
    (txid,) = struct.unpack(">H", query[:2])
    (orig_flags,) = struct.unpack(">H", query[2:4])
    flags = 0x8000 | (orig_flags & 0x0100) | 0x0080 | (rcode & 0x0F)
    return struct.pack(">HHHHHH", txid, flags, 0, 0, 0, 0)


def forward(query: bytes, timeout: float = 4.0) -> bytes | None:
    """Pass a query to the first upstream resolver that answers."""
    for resolver in UPSTREAM_RESOLVERS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(query, (resolver, 53))
                reply, _ = sock.recvfrom(4096)
                return reply
        except OSError:
            continue
    return None


def start_captive_http(bind: str, emit) -> None:
    """Serve the connectivity-check page on port 80, in a background thread.

    Answers every path, not just /hotspot-detect.html: iOS varies the URL it
    probes between versions, and a 404 on an unexpected path reads as a failed
    check exactly like no server at all.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(CAPTIVE_BODY)))
            self.end_headers()
            self.wfile.write(CAPTIVE_BODY)
            emit(f"  {self.client_address[0]}  HTTP {self.path}  [captive-ok]")

        def log_message(self, *a):
            pass

    try:
        httpd = ThreadingHTTPServer((bind, 80), Handler)
    except OSError as exc:
        print(f"  captive HTTP: could NOT bind :80 ({exc}) -- iOS will still "
              f"think this network has no internet.")
        return
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"captive-portal responder on {bind}:80")


def serve(bind: str, port: int, target_ip: str, domains: set[str], verbose: bool,
          log_path: str | None = None, captive: bool = False,
          blocked: set[str] | None = None) -> None:
    # Tee to a file when asked. Opened line-buffered and flushed per write: the
    # whole point is reading it live from another process while this one runs,
    # and a half-written buffer would show nothing at exactly the moment it
    # matters.
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1) if log_path else None

    def emit(line: str) -> None:
        if verbose:
            print(line)
        if log_fh:
            log_fh.write(line + "\n")
            log_fh.flush()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # NOT SO_REUSEADDR on Windows. There it does not mean what it means on
    # Unix: it lets a SECOND process bind a port this one already holds, and
    # the OS then splits incoming datagrams between them arbitrarily. Two
    # responders each answering some queries is close to undebuggable from the
    # phone's side -- it looks like an intermittent network fault.
    # SO_EXCLUSIVEADDRUSE makes the second instance fail loudly at bind instead.
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind, port))
    except PermissionError:
        sys.exit(f"cannot bind {bind}:{port} -- run this shell as administrator.")
    except OSError as exc:
        hint = (
            "Another instance of this script is probably already running -- that\n"
            "is the most common cause, and it is now reported instead of the two\n"
            "silently splitting incoming queries between them."
        )
        if port == 53:
            hint += (
                "\nOtherwise, on Windows the usual culprit is the 'Internet Connection\n"
                "Sharing (ICS)' service, whose DNS proxy binds UDP 53. Turn it off\n"
                "with tools\\setup-hotspot.ps1 and retry."
            )
        sys.exit(f"cannot bind {bind}:{port}: {exc}\n{hint}")

    print(f"DNS responder on {bind}:{port}")
    print(f"  {', '.join(sorted(domains))}  ->  {target_ip}")
    print(f"  everything else -> {', '.join(UPSTREAM_RESOLVERS)}")
    print()
    print("On the iPhone: Settings > Wi-Fi > (i) > Configure DNS > Manual")
    print(f"  remove all entries, add: {target_ip}")
    print()
    if captive:
        start_captive_http(bind, emit)
    print("Ctrl-C to stop.\n")

    def handle(packet: bytes, addr) -> None:
        parsed = parse_question(packet)
        if parsed is None:
            sock.sendto(build_rcode_response(packet, 1), addr)   # FORMERR
            return
        qname, qtype, qclass = parsed
        bare = qname.lower().rstrip(".")

        # NXDOMAIN for encrypted-resolver bootstrap names, before anything else.
        # Matched on suffix too: providers hang per-profile subdomains off the
        # same zone, and letting one of those through defeats the whole point.
        if blocked and (bare in blocked or any(bare.endswith("." + b) for b in blocked)):
            sock.sendto(build_rcode_response(packet, 3), addr)   # NXDOMAIN
            emit(f"  {addr[0]}  {qname}  [BLOCKED: doh-bootstrap]")
            return

        hit = bare in domains

        # Only A/IN is answered locally. An AAAA for a redirected name must be
        # refused with an EMPTY NOERROR, never forwarded: forwarding hands the
        # phone Cygames' real IPv6 address, it prefers v6 over v4, and the
        # redirect is silently bypassed while the A record you are watching in
        # this log looks perfectly correct.
        if hit and qtype == TYPE_A and qclass == CLASS_IN:
            sock.sendto(build_a_response(packet, target_ip), addr)
            emit(f"  {addr[0]}  {qname}  -> {target_ip}  [redirected]")
            return
        if hit:
            sock.sendto(build_rcode_response(packet, 0), addr)   # NOERROR, no answers
            emit(f"  {addr[0]}  {qname}  type={qtype}  [empty, not forwarded]")
            return

        reply = forward(packet)
        if reply is None:
            sock.sendto(build_rcode_response(packet, 2), addr)   # SERVFAIL
            emit(f"  {addr[0]}  {qname}  [upstream failed]")
            return
        sock.sendto(reply, addr)
        emit(f"  {addr[0]}  {qname}  [forwarded]")

    while True:
        try:
            packet, addr = sock.recvfrom(4096)
        except KeyboardInterrupt:
            print("\nstopped.")
            return
        # One thread per query: forwarding blocks for up to 4s per resolver, and
        # a single slow upstream lookup must not stall the redirect answers the
        # game is waiting on.
        threading.Thread(target=handle, args=(packet, addr), daemon=True).start()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--ip", default=None,
                    help="address to hand out (default: this machine's LAN IP)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=53)
    ap.add_argument("--extra-domain", action="append", default=[],
                    help="additional name to redirect; repeatable")
    ap.add_argument("--quiet", action="store_true", help="do not log each query")
    ap.add_argument("--captive", action="store_true",
                    help="also answer Apple's connectivity check locally (DNS + an "
                         "HTTP responder on :80) so iOS does not decide this network "
                         "has no internet and stop using it. Needed when the hotspot "
                         "genuinely has no upstream -- which is fine, because the game "
                         "only ever talks to this machine.")
    ap.add_argument("--block-doh", action="store_true",
                    help="answer NXDOMAIN for the names a client uses to find an "
                         "encrypted DNS resolver (DDR, dns.google, cloudflare-dns "
                         "and friends), forcing it back onto plain DNS that this "
                         "responder actually sees.")
    ap.add_argument("--log-file", default=None,
                    help="also append every query line to this file. The console "
                         "output is the only evidence of what the phone actually "
                         "asked for, and it is not readable from anywhere else -- "
                         "this makes it inspectable after the fact.")
    args = ap.parse_args()

    # Line-buffer stdout: piped to a file it would otherwise block-buffer, and
    # the query log is the main thing you watch to tell "the phone never reached
    # me" apart from "it reached me and I answered".
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    target_ip = args.ip or detect_lan_ip()
    domains = {d.lower().rstrip(".") for d in DEFAULT_DOMAINS + args.extra_domain}
    if args.captive:
        domains |= {d.lower() for d in CAPTIVE_DOMAINS}

    if target_ip.startswith("127."):
        sys.exit(f"refusing to hand out {target_ip} -- a phone cannot reach your "
                 "loopback. Pass --ip with this machine's LAN address.")
    if target_ip.startswith("172.20.10."):
        print("WARNING: 172.20.10.x is the iOS Personal Hotspot subnet, which means\n"
              "         this PC is tethered TO the phone. That topology cannot work:\n"
              "         the phone is the gateway, and its own traffic never passes\n"
              "         through this machine.\n"
              "         Turn it around: run Windows Mobile Hotspot on this PC and\n"
              "         join it from the phone. See tools/setup-hotspot.ps1.\n")

    try:
        serve(args.bind, args.port, target_ip, domains, verbose=not args.quiet,
              log_path=args.log_file, captive=args.captive,
              blocked={d.lower() for d in DOH_BOOTSTRAP_DOMAINS} if args.block_doh else None)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
