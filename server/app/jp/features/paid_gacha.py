
from __future__ import annotations

import functools

from ... import master_data
from ...handlers import gacha

SSR_BANNER = 5
PAID_BUTTON = 1


@functools.lru_cache(maxsize=None)
def _row(gacha_id: int) -> dict:
    return dict(master_data.query_one(
        "SELECT type, paid_gacha_button_type, draw_limit FROM gacha_data WHERE id=?",
        (gacha_id,)) or {})


def paid_limit(gacha_id: int):
    row = _row(int(gacha_id))
    if row.get("type") == SSR_BANNER:
        return (1, int(row.get("draw_limit") or 1))
    if row.get("paid_gacha_button_type") == PAID_BUTTON:
        return (gacha.DRAW_PAID, int(row.get("draw_limit") or 1))
    return None


def limited_banner(banner: dict) -> dict:
    limit = paid_limit(banner["gacha_id"])
    if not limit:
        return banner
    banner["paid_limit"] = limit
    if limit[0] == gacha.DRAW_PAID:
        banner["paid_button"] = True
    else:
        banner["counts_pity"] = False
    return banner


if limited_banner not in gacha.BANNER_HOOKS:
    gacha.BANNER_HOOKS.append(limited_banner)


def decorate_index(full_state: dict, data: dict) -> None:
    listed = []
    for g in data.get("gacha_info_list") or []:
        limit = paid_limit(g["id"])
        remain = max(0, limit[1] - gacha.paid_draws_used(full_state, g["id"])) if limit else 0
        if limit and limit[0] != gacha.DRAW_PAID and not remain:
            continue
        listed.append({**g, "remain_paid_draw_num": remain})
    data["gacha_info_list"] = listed
