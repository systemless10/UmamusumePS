
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from fastapi import FastAPI, Request, Response

from .. import region
from ..admin_lifecycle import lifespan

if region.PROCESS_REGION != "jp":
    raise RuntimeError("app.jp.server needs UMA_REGION=jp (use ./start-server.sh --region jp)")

_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "server_jp.log", maxBytes=20_000_000, backupCount=3, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler(), _file_handler])
logging.getLogger().handlers[0].setLevel(os.environ.get("UMA_LOG_LEVEL", "INFO").upper())
log = logging.getLogger("uma-server")

from .. import dispatch as pipeline
from .. import scenarios
from ..result_codes import refuse
from . import arming, endpoints, features, keys, schema, story_ids, transport

pipeline.HANDLERS.update(endpoints.HANDLERS)
pipeline.HANDLERS.update(features.HANDLERS)
pipeline.REQUEST_GUARDS.append(features.trainer_skill.before)

_SERVED_FAMILIES = {"single_mode"} | {s.endpoint_prefix for s in scenarios.all_scenarios()}


def _refuse_unserved_scenario(payload: dict) -> dict:
    return refuse()


def _guard_scenario_family(endpoint: str) -> None:
    family = endpoint.split("/", 1)[0]
    if (family.startswith("single_mode_") and family not in _SERVED_FAMILIES
            and endpoint not in pipeline.HANDLERS):
        log.warning("JP %s: scenario family %s is not served -- refusing", endpoint, family)
        pipeline.HANDLERS[endpoint] = _refuse_unserved_scenario


def flatten_request_common(payload: dict) -> dict:
    for key, inner in list(payload.items()):
        if key.endswith("_request_common") and isinstance(inner, dict):
            for k, v in inner.items():
                payload.setdefault(k, v)
    return payload


app = FastAPI(title="Umamusume private server (JP)", lifespan=lifespan)
from .. import custom_support, custom_support_http
app.include_router(custom_support_http.router)
from .. import profile_images
app.include_router(profile_images.router)
arming.start()


def finish(resp: dict, endpoint: str, payload: dict | None = None) -> dict:
    handled = endpoint in pipeline.HANDLERS or pipeline.registry.get(endpoint) is not None
    resp = features.support_cards.apply(endpoint, payload, resp)
    if (resp.get("data_headers") or {}).get("result_code", 1) == 1:
        features.succession.mark_unrecorded(resp.get("data"))
        features.profile_slots.fill(endpoint, resp)
        resp = schema.conform(endpoint, resp, handled)
        story_ids.apply(resp)
    dh = resp.setdefault("data_headers", {})
    if endpoint in endpoints.SERVER_LIST_ENDPOINTS:
        dh["server_list"] = endpoints.SERVER_LIST
    else:
        dh.pop("server_list", None)
    return resp


def _played_branch(endpoint: str, payload: dict) -> int | None:
    if not endpoint.endswith("/check_event") or not endpoint.startswith("single_mode"):
        return None
    from .. import state as state_store
    from ..handlers import single_mode_team
    full_state = state_store.get_state(payload.get("viewer_id")) or {}
    branch = single_mode_team._served_branch(full_state, payload.get("event_id"),
                                             payload.get("choice_number"))
    return int(branch or 1)


def handle(endpoint: str, payload: dict, sid) -> dict:
    payload = flatten_request_common(payload)
    _guard_scenario_family(endpoint)
    if not custom_support.preflight(endpoint, payload):
        return finish(pipeline.handle_decoded(endpoint, payload, sid), endpoint, payload)
    branch = _played_branch(endpoint, payload)
    noted = features.skill_upgrade.before(endpoint, payload)
    noted_sp = features.succession.before(endpoint, payload)
    resp = finish(pipeline.handle_decoded(endpoint, payload, sid), endpoint, payload)
    features.skill_upgrade.after(endpoint, payload, resp, noted)
    features.succession.after(endpoint, payload, resp, noted_sp)
    features.const_walking.after(endpoint, payload, resp)
    features.plushie.after(endpoint, payload, resp)
    features.passes.after(endpoint, payload, resp)
    features.auto_play.after(endpoint, payload, resp)
    features.skill_filter.after(endpoint, payload, resp)
    features.exhibition.after(endpoint, payload, resp)
    features.archive.after(endpoint, payload, resp)
    features.trainer_skill.after(endpoint, payload, resp)
    data = resp.get("data")
    if branch is not None and isinstance(data, dict):
        data["select_index_info_array"] = [{"branch_number": 1, "select_index": branch}]
    return resp


@app.post("/{endpoint_path:path}")
async def dispatch(endpoint_path: str, request: Request) -> Response:
    endpoint = endpoint_path.strip("/")
    if endpoint.startswith("umamusume/"):
        endpoint = endpoint[len("umamusume/"):]
    raw = await request.body()
    headers = dict(request.headers)
    profile_images.set_request_base(headers)

    try:
        decoded = transport.open_request(raw, headers, keys.private_scalar(), arming.SESSIONS)
    except transport.WrongKey as exc:
        log.error("JP %s: %s -- is the Hachimi plugin loaded and armed?", endpoint, exc)
        return Response(status_code=400)
    except transport.BadRequest as exc:
        log.error("JP %s: %s", endpoint, exc)
        return Response(status_code=400)

    endpoints.remember_session(decoded)
    token = endpoints.CURRENT_REQUEST.set(decoded)
    try:
        payload = decoded.payload if isinstance(decoded.payload, dict) else {}
        with custom_support.request_context(headers, payload):
            resp = custom_support.project(handle(endpoint, payload, decoded.sid))
    finally:
        endpoints.CURRENT_REQUEST.reset(token)

    wire = transport.seal_reply(resp, decoded)
    body, extra_headers = transport.encode_http_body(wire, headers.get("accept-encoding", ""))
    return Response(content=body, media_type="application/x-msgpack; charset=utf-8",
                    headers={"Cache-Control": "max-age=0, no-cache, no-store",
                             "Pragma": "no-cache", **extra_headers})
