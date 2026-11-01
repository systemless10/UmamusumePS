from __future__ import annotations
import json
from pathlib import Path

TABLES = json.loads(Path(__file__).with_name('master_data.json').read_text())


def rows(name):
    return TABLES.get(name, [])


def schedule(turn):
    return next((r for r in rows('single_mode_15_schedule') if r['turn_num'] == turn), None)


def energy(command, level):
    row = next((r for r in rows('single_mode_training_effect')
                if r['command_id'] == command and r['sub_id'] == level and r['target_type'] == 10), None)
    if row:
        return row['effect_value']
    base = next((r['effect_value'] for r in rows('single_mode_training_effect')
                 if r['command_id'] == command and r['target_type'] == 10), -25)
    return base if command >= 5100 or command == 106 else base - (0,1,2,4,6)[min(5,max(1,level))-1]
