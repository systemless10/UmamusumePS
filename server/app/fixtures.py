
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import region

CAPTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "capture_06d26c"

_DUMP_CANDIDATES = [
    p for p in [
        os.environ.get("UMADUMPY_DUMPS_DIR"),
        r"C:\Users\Trainer\Documents\UmaDumpy-main\dumps",
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
    _response_json: str | None = field(default=None, repr=False, compare=False)

    def response_copy(self) -> dict:
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
        if self.capture_dir is None:
            return
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
        if self.umadumpy_dumps_dir is None or not self.umadumpy_dumps_dir.exists():
            return

        for session_dir in sorted(self.umadumpy_dumps_dir.iterdir()):
            tx_dir = session_dir / "transactions"
            if not tx_dir.is_dir():
                continue

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
                endpoint = endpoint.strip("/")
                if endpoint.startswith("umamusume/"):
                    endpoint = endpoint[len("umamusume/") :]

                response = (doc.get("response") or {}).get("data")
                if not response or response.get("response_code") != 1:
                    continue

                request = (doc.get("request") or {}).get("data") or {}
                ts = (doc.get("timing") or {}).get("request_epoch_ms", 0) / 1000.0
                response_data = response.get("data") or {}

                if "race_instance_id" in request and "entry_chara_array" in request:
                    race_start_request = request
                    race_start_request_ts = ts
                if "race_result_info" in response_data:
                    race_start_response = response

                if endpoint == "practice_race/race_start":
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
        pairs = self.by_endpoint.get(endpoint, [])
        for pair in pairs:
            if all(pair.request.get(k) == v for k, v in request_fields.items()):
                return pair
        return pairs[0] if pairs else None

    def find_after(self, endpoint: str, ts: float) -> Pair | None:
        pairs = self.by_endpoint.get(endpoint, [])
        for pair in pairs:
            if pair.ts >= ts:
                return pair
        return pairs[-1] if pairs else None


store = (FixtureStore() if region.PROCESS_REGION == "global"
         else FixtureStore(capture_dir=None, umadumpy_dumps_dir=None))


def _load_templates(directory: Path) -> FixtureStore:
    out = FixtureStore(capture_dir=None, umadumpy_dumps_dir=None)
    for fp in sorted(directory.glob("*.json")) if directory.is_dir() else ():
        doc = json.loads(fp.read_text(encoding="utf-8"))
        out.by_endpoint.setdefault(doc["endpoint"], []).append(
            Pair(ts=0.0, endpoint=doc["endpoint"], req_id=None,
                 request=doc.get("request") or {}, response=doc["response"]))
    return out


templates = (store if region.PROCESS_REGION == "global"
             else _load_templates(region.seeds_dir() / "career"))
