"""Redaction rules for MITM capture records (see tools/capture_proxy.py).

A capture record is one JSON transaction dumped by the proxy: request/response
headers, the decoded msgpack request payload, the decoded response, and (until
this module strips it) a base64 copy of the still-encrypted wire bytes.

WHAT GETS REDACTED AND WHY
---------------------------
* request_headers["SID"], request_headers["ViewerID"], response.data_headers
  ["sid"] -- session auth tokens. A live SID is a bearer credential: whoever
  has it can act as the account until it rotates.
* request["password"], ["credential"] -- login secrets.
* request["device_token"], ["dmm_onetime_token"], ["steam_session_ticket"],
  ["adid"] -- platform auth/tracking tokens.
* request["device_id"], ["device_name"], ["graphics_device_name"],
  ["ip_address"], ["carrier"], ["keychain"], ["steam_id"], ["dmm_viewer_id"],
  udid_hex -- device/network fingerprint, identifies the real machine/person.
* raw["request_b64"] / raw["response_b64"] -- dropped entirely, not redacted
  in place. These are the still-encrypted wire bytes; server/app/crypto.py in
  this same repo can decrypt them (the AES key is derived only from the udid
  embedded in the blob itself), so leaving them next to a "redacted" record
  would just let anyone with the repo recover everything above from the raw
  bytes. Nothing in this codebase reads raw_b64 back out of a capture file,
  so dropping it is safe.
* viewer_id / target_viewer_id / input_viewer_id / friend_viewer_id, wherever
  they appear in the document (top-level request, response.data_headers,
  and arbitrarily nested inside response.data -- team rosters, opponent info,
  friend lookups, etc.) -- these are real Cygames account identifiers, mapped
  to a stable per-real-id synthetic replacement (900000000001, ...002, ...)
  so the *shape* of the capture (which two records share an account) survives
  redaction even though the real id doesn't.
* Any "name" / "request_user_name" sitting next to a redacted viewer-id key in
  the same dict -- these are the trainer handles those endpoints resolve the
  id to (account lookup, friend search, ...); pseudonymized in lockstep with
  the id they're attached to.

WHAT DOES NOT GET TOUCHED
--------------------------
response.data is otherwise left alone: card/deck/race/item names, ids, and
game state are not credentials and multiple handlers (see
server/app/handlers/idle_single_mode.py's _default_reserved_race_info, which
reads response.data.reserved_race_info out of a specific capture file at
runtime) depend on that content being byte-for-byte what the real server
returned.

Idempotent: a record whose SID header is already "REDACTED" is left alone, so
re-running this over an already-redacted tree, or a mixed tree, is safe.
"""

from __future__ import annotations

SECRET_STRING_KEYS = {
    "device_token", "device_id", "device_name", "graphics_device_name",
    "ip_address", "carrier", "keychain", "dmm_viewer_id", "dmm_onetime_token",
    "steam_id", "steam_session_ticket", "steam_session_auth_ticket",
    "password", "credential", "adid",
}

VIEWER_ID_KEYS = {"viewer_id", "target_viewer_id", "input_viewer_id", "friend_viewer_id"}
NAME_SIBLING_KEYS = {"name", "request_user_name"}

_REDACTED = "REDACTED"
_ID_BASE = 900000000000


class IdMapper:
    """Stable real-viewer-id -> synthetic-id mapping, shared across a whole
    redaction run so the same real account maps to the same fake id in every
    file it appears in."""

    def __init__(self) -> None:
        self._map: dict[int, int] = {}
        self._next = _ID_BASE + 1

    def synth(self, real_id: int) -> int:
        if real_id in self._map:
            return self._map[real_id]
        fake = self._next
        self._next += 1
        self._map[real_id] = fake
        return fake

    @property
    def mapped_count(self) -> int:
        return len(self._map)

    def pseudo_name_for_synth(self, synth_id: int) -> str:
        return f"Trainer{synth_id % 1_000_000:06d}"


def is_already_redacted(rec: dict) -> bool:
    return rec.get("request_headers", {}).get("SID") == _REDACTED


def _redact_viewer_ids_and_names(obj, mapper: IdMapper) -> None:
    """Recursively walk the whole document (arbitrary nesting -- team
    rosters, opponent_info, friend lookups) replacing viewer-id-family
    values and any name/request_user_name sitting next to one."""
    if isinstance(obj, dict):
        mapped_here = False
        for key in list(obj.keys()):
            val = obj[key]
            if key in VIEWER_ID_KEYS and isinstance(val, int) and val:
                obj[key] = mapper.synth(val)
                mapped_here = True
            elif key in VIEWER_ID_KEYS and isinstance(val, str) and val:
                obj[key] = _REDACTED
        if mapped_here:
            # use whichever viewer-id-family value is present to key the name
            ref_id = next((obj[k] for k in VIEWER_ID_KEYS
                            if isinstance(obj.get(k), int) and obj.get(k)), None)
            if ref_id is not None:
                for nk in NAME_SIBLING_KEYS:
                    if isinstance(obj.get(nk), str) and obj[nk]:
                        obj[nk] = mapper.pseudo_name_for_synth(ref_id)
        for val in obj.values():
            _redact_viewer_ids_and_names(val, mapper)
    elif isinstance(obj, list):
        for item in obj:
            _redact_viewer_ids_and_names(item, mapper)


def redact_record(rec: dict, mapper: IdMapper) -> bool:
    """Redact one capture record in place. Returns True if anything changed."""
    if is_already_redacted(rec):
        return False

    changed = False

    if "raw" in rec:
        del rec["raw"]
        changed = True

    if rec.get("udid_hex"):
        rec["udid_hex"] = _REDACTED
        changed = True

    headers = rec.get("request_headers")
    if isinstance(headers, dict):
        for k in ("SID", "ViewerID"):
            if headers.get(k):
                headers[k] = _REDACTED
                changed = True

    req = rec.get("request")
    if isinstance(req, dict):
        for k in SECRET_STRING_KEYS:
            if req.get(k):
                req[k] = _REDACTED
                changed = True

    data_headers = (rec.get("response") or {}).get("data_headers")
    if isinstance(data_headers, dict) and data_headers.get("sid"):
        data_headers["sid"] = _REDACTED
        changed = True

    before = str(rec)
    _redact_viewer_ids_and_names(rec, mapper)
    if str(rec) != before:
        changed = True

    return changed
