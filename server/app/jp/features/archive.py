
from __future__ import annotations

from ... import state as state_store
from ...handlers import archive_directory, trained_chara

HANDLERS: dict = {}


def after(endpoint: str, payload: dict, resp: dict) -> None:
    data = resp.get("data")
    if not isinstance(data, dict) or (resp.get("data_headers") or {}).get("result_code", 1) != 1:
        return
    if endpoint == "note/index":
        viewer_id = payload.get("viewer_id")
        roster = trained_chara._get_or_seed_roster(viewer_id)
        full_state = state_store.get_state(viewer_id) or {}
        data["top_directory_card_array"] = archive_directory.top_per_card(
            viewer_id, archive_directory.pool(full_state, roster))
        data.pop("directory_card_array", None)
    elif endpoint == "trained_chara/remove":
        kept, deleted = archive_directory.LAST_RETIRE.get() or ([], [])
        archive_directory.LAST_RETIRE.set(None)
        data["update_trained_chara_id_array"] = list(kept)
        data["remove_trained_chara_id_array"] = list(deleted)
