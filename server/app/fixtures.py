"""
Loads the captured request/response pairs from the headless-play capture
directory and indexes them by API endpoint, so unimplemented or
not-yet-simulated endpoints can fall back to replaying real captured
responses (with a few obviously-dynamic fields patched) instead of erroring.

This is a bootstrapping tool, not the end state: as real logic gets written
for an endpoint (see app/handlers/), that handler should stop relying on
fixture replay for anything that needs to react to player choices.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Moved out of the repo root (was ../../20260717_124934_06d26c) into a
# dedicated, tracked fixtures directory so the raw capture folders can be
# gitignored without taking the server's replay source down with them.
CAPTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "capture_06d26c"

# UmaDumpy (github.com/viri-miryoku/UmaDumpy) sessions -- a second, richer
# capture source with its own schema (one file per transaction, request+
# response paired already, no req_id matching needed). Every finalized
# session (one with a transactions/ dir) under here is indexed automatically,
# so a fresh capture just needs a server restart to pick it up.
#
# This is by far the LARGER capture source (~1,616 transactions across 30
# sessions, vs 385 pairs in the 06d26c set) and it holds the FRESHER captures,
# so losing it silently degrades every fixture-replayed endpoint to whatever
# the older 06d26c set happens to have. It went missing exactly that way once:
# the path below was hardcoded to the Windows checkout, so the Linux port
# indexed 385 pairs instead of ~2,000 and served stale build responses with no
# error anywhere (see docs/LINUX_LOGIN_FIX.md). Resolved by search now, with
# an env override, for the same reason app/master_data.py does it.
_DUMP_CANDIDATES = [
    p for p in [
        os.environ.get("UMADUMPY_DUMPS_DIR"),
        r"C:\Users\Systemless\Documents\UmaDumpy-main\dumps",
        str(Path(__file__).resolve().parents[3] / "UmaDumpy-main" / "dumps"),
        os.path.expanduser("~/Documents/Projects/UmaDumpy-main/dumps"),
        os.path.expanduser("~/Documents/UmaDumpy-main/dumps"),
    ] if p
]


def _resolve_dumps_dir() -> Path:
    for candidate in _DUMP_CANDIDATES:
        if Path(candidate).is_dir():
            return Path(candidate)
    return Path(_DUMP_CANDIDATES[0])


UMADUMPY_DUMPS_DIR = _resolve_dumps_dir()


@dataclass
class Pair:
    ts: float
    endpoint: str
    req_id: str | None
    request: dict
    response: dict
    # Lazily-built JSON text of `response`, for response_copy() below. Not a
    # constructor argument -- every existing Pair(...) call site is unchanged.
    _response_json: str | None = field(default=None, repr=False, compare=False)

    def response_copy(self) -> dict:
        """An independent copy of `response`, parsed from JSON text rather than
        deep-copied.

        Career fixtures are big -- the captured single_mode_team/finish
        response is 2.9 MB -- and every handler that serves one deep-copies it
        first so it can patch the copy without corrupting the shared fixture.
        copy.deepcopy has to walk the entire object graph (394,000 objects for
        that one, 109 ms); re-parsing the JSON text it was loaded from produces
        exactly the same structure in 48 ms. The text is built once per fixture
        per process and reused after that.

        Fixtures are pure decoded JSON (see _load / _load_umadumpy), so this is
        equivalent to a deepcopy for every value they can contain.
        """
        if self._response_json is None:
            self._response_json = json.dumps(self.response)
        return json.loads(self._response_json)


class FixtureStore:
    def __init__(
        self,
        capture_dir: Path = CAPTURE_DIR,
        umadumpy_dumps_dir: Path = UMADUMPY_DUMPS_DIR,
    ):
        self.capture_dir = capture_dir
        self.umadumpy_dumps_dir = umadumpy_dumps_dir
        self.by_endpoint: dict[str, list[Pair]] = {}
        self._load()
        self._load_umadumpy()

    def _load(self) -> None:
        outgoing_by_key: dict[tuple[str, str], dict] = {}
        incoming: list[dict] = []

        for fp in sorted(self.capture_dir.glob("*.json")):
            try:
                doc = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            direction = doc.get("direction")
            endpoint = doc.get("endpoint")
            req_id = doc.get("req_id")
            if not endpoint:
                continue

            if direction == "REQ":
                outgoing_by_key[(endpoint, req_id)] = doc
            elif direction == "RES":
                incoming.append(doc)

        for res_doc in incoming:
            endpoint = res_doc["endpoint"]
            req_id = res_doc.get("req_id")
            req_doc = outgoing_by_key.get((endpoint, req_id))
            if req_doc is None:
                continue

            pair = Pair(
                ts=req_doc.get("ts", 0.0),
                endpoint=endpoint,
                req_id=req_id,
                request=req_doc.get("data", {}).get("payload", {}),
                response=res_doc.get("data", {}),
            )
            self.by_endpoint.setdefault(endpoint, []).append(pair)

        self._sort()

    def _load_umadumpy(self) -> None:
        if not self.umadumpy_dumps_dir.exists():
            return

        for session_dir in sorted(self.umadumpy_dumps_dir.iterdir()):
            tx_dir = session_dir / "transactions"
            if not tx_dir.is_dir():
                continue

            # UmaDumpy's Frida hook has a URL/body attribution bug specific
            # to the practice_race flow: the real race_start request
            # (race_instance_id + course/uma selection) and its real
            # response (the actual simulation, race_result_info etc.) each
            # land under the URL label of some OTHER practice_race call
            # (get_follow_user_data / get_preset_array / race_start itself
            # depending on session) instead of together in one clean slot --
            # confirmed across 4 independent sessions, including at the raw
            # msgpack level, so it's a capture-tool bug, not corrupt data.
            # Collect the real request half and real response half by body
            # shape across the whole session (they're not always in the same
            # transaction slot as each other) and pair them manually instead
            # of trusting any single slot's own request/response pairing or
            # label for this endpoint.
            race_start_request = None
            race_start_request_ts = None
            race_start_response = None

            for fp in sorted(tx_dir.glob("*.json")):
                try:
                    doc = json.loads(fp.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                endpoint = doc.get("endpoint")
                if not endpoint:
                    continue
                # UmaDumpy endpoints are "/umamusume/{ep}"; our internal
                # convention (and the older capture set) uses bare "{ep}".
                endpoint = endpoint.strip("/")
                if endpoint.startswith("umamusume/"):
                    endpoint = endpoint[len("umamusume/") :]

                response = (doc.get("response") or {}).get("data")
                if not response or response.get("response_code") != 1:
                    continue  # skip incomplete/errored transactions

                request = (doc.get("request") or {}).get("data") or {}
                ts = (doc.get("timing") or {}).get("request_epoch_ms", 0) / 1000.0
                response_data = response.get("data") or {}

                if "race_instance_id" in request and "entry_chara_array" in request:
                    race_start_request = request
                    race_start_request_ts = ts
                if "race_result_info" in response_data:
                    race_start_response = response

                if endpoint == "practice_race/race_start":
                    # Never trust this label's own content directly -- the
                    # real pair is assembled below from whichever slots
                    # actually hold the real request/response shapes.
                    continue

                pair = Pair(
                    ts=ts,
                    endpoint=endpoint,
                    req_id=doc.get("transaction_id"),
                    request=request,
                    response=response,
                )
                self.by_endpoint.setdefault(endpoint, []).append(pair)

            if race_start_request is not None and race_start_response is not None:
                self.by_endpoint.setdefault("practice_race/race_start", []).append(
                    Pair(
                        ts=race_start_request_ts,
                        endpoint="practice_race/race_start",
                        req_id=None,
                        request=race_start_request,
                        response=race_start_response,
                    )
                )

        self._sort()

    def _sort(self) -> None:
        for pairs in self.by_endpoint.values():
            pairs.sort(key=lambda p: p.ts)

    def endpoints(self) -> list[str]:
        return sorted(self.by_endpoint.keys())

    def all_for(self, endpoint: str) -> list[Pair]:
        return self.by_endpoint.get(endpoint, [])

    def first(self, endpoint: str) -> Pair | None:
        pairs = self.by_endpoint.get(endpoint)
        return pairs[0] if pairs else None

    def find(self, endpoint: str, **request_fields) -> Pair | None:
        """Best-effort match: prefer a pair whose request matches all given
        fields exactly; otherwise fall back to the first capture for that
        endpoint."""
        pairs = self.by_endpoint.get(endpoint, [])
        for pair in pairs:
            if all(pair.request.get(k) == v for k, v in request_fields.items()):
                return pair
        return pairs[0] if pairs else None

    def find_after(self, endpoint: str, ts: float) -> Pair | None:
        """The earliest-captured pair for endpoint at or after ts. For
        multi-step flows (race_entry -> race_start -> race_end -> race_out)
        where later steps' requests don't carry an identifying field (no
        program_id on race_start, etc.), this correlates "the real call that
        happened right after this one in the original session" using capture
        order instead."""
        pairs = self.by_endpoint.get(endpoint, [])
        for pair in pairs:
            if pair.ts >= ts:
                return pair
        return pairs[-1] if pairs else None


store = FixtureStore()
