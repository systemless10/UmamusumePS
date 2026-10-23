#!/usr/bin/env python3
import argparse
import hashlib
import json
import sqlite3
import struct
from pathlib import Path
import apsw
import UnityPy

META_KEY = '9c2bab97bcf8c0c4f1a9ea7881a213f6c9ebf9d8d4c6a8e43ce5a259bde7e9fd'
_DB_BASE = bytes([0xF1,0x70,0xCE,0xA4,0xDF,0xCE,0xA3,0xE1,0xA5,0xD8,0xC7,0x0B,0xD1])
_GLOBAL_DB = bytes([0x36,0x23,0x6b,0x4c,0x2a,0x39,0x21,0x75,0x52,0x26,0x32,0x76,0x25,0x50,0x3f,0x35,0x5d,0x77,
                    0x58,0x6d,0x40,0x71,0x38,0x5e,0x4c,0x31,0x28,0x74,0x29,0x59,0x37,0x24,0x53])
GLOBAL_META_KEY = bytes(b ^ _DB_BASE[i % 13] for i, b in enumerate(_GLOBAL_DB)).hex()
BASE_KEYS = bytes([0x53,0x2B,0x46,0x31,0xE4,0xA7,0xB9,0x47,0x3E,0x7C,0xFB])
EVENT_TYPES = dict(enumerate(['Corner','GroundChange','Straight','LaneMaxChange','FirstBgmLandscape','SecondBgmLandscape','JikkyoTrigger','MoveLanePoint','FirstBgm','SecondBgm','EventCrowd','Slope']))

def floats(value):
    if isinstance(value, float):
        bits = struct.pack('<f', value)
        assert struct.unpack('<f', bits)[0] == value
        return {'decimal': value, 'float32_hex': f'0x{struct.unpack("<I", bits)[0]:08x}'}
    if isinstance(value, dict):
        return {k: floats(v) for k,v in value.items()}
    if isinstance(value, list):
        return [floats(v) for v in value]
    return value

def bundle(persistent, row):
    name, digest, key = row
    raw = (persistent/'dat'/digest[:2]/digest).read_bytes()
    source = {'asset_path': name, 'bundle_hash': digest, 'sha256': hashlib.sha256(raw).hexdigest()}
    if key:
        key_bytes = struct.pack('<q', key)
        xor = bytes(b ^ k for b in BASE_KEYS for k in key_bytes)
        raw = raw[:256] + bytes(b ^ xor[i % 88] for i,b in enumerate(raw[256:], 256))
    env = UnityPy.load(raw)
    scripts = {o.path_id: o.read_typetree() for o in env.objects if o.type.name == 'MonoScript'}
    objects = []
    for o in env.objects:
        if o.type.name != 'MonoBehaviour':
            continue
        tree = o.read_typetree()
        script = scripts.get(tree['m_Script']['m_PathID'], {})
        objects.append({'path_id': o.path_id, 'class': script.get('m_ClassName'), 'namespace': script.get('m_Namespace'), 'fields': tree})
    return {'source': source, 'objects': objects}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--persistent', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('server/data/race_param_define.json'))
    parser.add_argument('--region', default='JP', choices=('JP', 'Global'))
    args = parser.parse_args()
    conn = apsw.Connection(str(args.persistent/'meta'), flags=apsw.SQLITE_OPEN_READONLY)
    conn.execute(f"PRAGMA hexkey='{GLOBAL_META_KEY if args.region == 'Global' else META_KEY}'")
    rows = list(conn.execute("SELECT n,h,e FROM a WHERE n='race/racemain/ast_race_paramdefine' OR n LIKE 'race/courseeventparam/%' OR n LIKE 'race/course/%/pos/%' ORDER BY n"))
    result = {'schema_version': 1, 'source_region': args.region, 'unitypy_version': UnityPy.__version__, 'float_encoding': 'Each serialized float is {decimal, float32_hex}; hex is IEEE-754 uint32 bits, not byte order.', 'course_event_types': EVENT_TYPES, 'race_param_define': None, 'course_sets': {}, 'lane_assets': {}}
    master = sqlite3.connect(f'file:{args.persistent / "master/master.mdb"}?mode=ro', uri=True)
    master.row_factory = sqlite3.Row
    for row in master.execute('SELECT * FROM race_course_set ORDER BY id'):
        record = dict(row)
        result['course_sets'][str(record['id'])] = {'master': record}
    for i,row in enumerate(rows):
        extracted = bundle(args.persistent,row)
        name = row[0]
        if name.endswith('ast_race_paramdefine'):
            result['race_param_define'] = floats(extracted)
        elif 'courseeventparam' in name:
            course = name.split('/')[2]
            result['course_sets'].setdefault(course,{})['event_asset'] = floats(extracted)
        else:
            result['lane_assets'][name] = floats(extracted)
        if i % 50 == 0: print(f'{i}/{len(rows)}', flush=True)
    assert result['race_param_define']
    for course in result['course_sets'].values():
        m = course.get('master')
        if not m: continue
        variant = 0 if m['turn'] == 4 else m.get('run_outside', 0)
        stem = f'race/course/{m["race_track_id"]}/pos/an_pos_race{m["race_track_id"]}_00_{m["distance"]:04}_{m["ground"]-1:02}_{m["inout"]-1}_{variant}'
        course['normal_lane_asset'] = stem if stem in result['lane_assets'] else None
        if stem in result['lane_assets']:
            fields = result['lane_assets'][stem]['objects'][0]['fields']
            key = fields['key']
            course['serialized_lane_start'] = {'position': {axis: key['value'+axis.upper()][0] for axis in ('x','y','z')}, 'rotation': key['rotation'][0]}
        course['gate_positions'] = {'status': 'runtime_derived_not_serialized_in_course_or_lane_asset'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
    print(f'Wrote {args.output}: {len(result["course_sets"])} course sets, {len(result["lane_assets"])} lanes')

if __name__ == '__main__': main()
