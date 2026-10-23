
from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER_VERSION = 100000002
FRAME_HZ = 15.0
SPEED_SCALE = 100.0
LANE_SCALE = 10000.0

@dataclass
class SkillEvent:
    horse_index: int
    skill_id: int
    t: float
    duration: float
    target_mask: int = 0


_SKILL_EVENT_PARAM_COUNT = 6
_SKILL_EVENT_STRUCT = struct.Struct("<hfBBiiiiii")


@dataclass
class RaceEvent:
    t: float
    type_id: int
    params: tuple


def _encode_race_events(events: list[RaceEvent]) -> bytes:
    sorted_events = sorted(events, key=lambda e: e.t)
    out = bytearray()
    out += struct.pack("<i", 0)
    out += struct.pack("<i", len(sorted_events))
    for e in sorted_events:
        param_count = len(e.params)
        record_length = 4 + 1 + 1 + param_count * 4
        out += struct.pack(f"<hfBB{param_count}i", record_length, e.t, e.type_id, param_count, *e.params)
    return bytes(out)


def _skill_event_to_race_event(e: SkillEvent) -> RaceEvent:
    duration = -1 if e.duration < 0 else round(e.duration * 10000)
    return RaceEvent(t=e.t, type_id=3, params=(
        e.horse_index, e.skill_id, duration, 0,
        ((e.target_mask or (1 << e.horse_index)) & 0xffffffff) -
        (0x100000000 if (e.target_mask or (1 << e.horse_index)) & 0x80000000 else 0), 0,
    ))


_FRAME_HORSE_STRUCT = struct.Struct("<fhhhbb")
_RESULT_STRUCT_HEAD = struct.Struct("<ifff")
_RESULT_STRUCT_TAIL = struct.Struct("<fBf")


@dataclass
class HorseFrame:
    distance: float
    lane_position: float
    speed: float
    hp: float
    temptation_mode: int = 0
    block_front_horse_index: int = -1


@dataclass
class HorseResult:
    finish_order: int
    finish_time: float
    finish_diff_time: float
    start_delay_time: float
    guts_order: int
    wiz_order: int
    last_spurt_start_distance: float
    running_style: int
    defeat: int
    finish_time_raw: float


def encode_horse_frame(buf: bytearray, hf: HorseFrame) -> None:
    def single(value: float) -> float:
        return struct.unpack('<f', struct.pack('<f', value))[0]

    lane_q = max(-32768, min(32767, int(single(single(hf.lane_position) * LANE_SCALE))))
    speed_q = max(-32768, min(32767, int(single(single(hf.speed) * SPEED_SCALE))))
    hp_q = max(-32768, min(32767, int(single(hf.hp))))
    block = hf.block_front_horse_index if -1 <= hf.block_front_horse_index <= 127 else -1
    buf.extend(_FRAME_HORSE_STRUCT.pack(
        hf.distance, lane_q, speed_q, hp_q, hf.temptation_mode, block
    ))


def encode_result(rec: HorseResult) -> bytes:
    buf = bytearray()
    buf.extend(struct.pack("<i", rec.finish_order))
    buf.extend(struct.pack("<f", rec.finish_time))
    buf.extend(struct.pack("<f", rec.finish_diff_time))
    buf.extend(struct.pack("<f", rec.start_delay_time))
    buf.append(max(0, min(255, rec.guts_order)))
    buf.append(max(0, min(255, rec.wiz_order)))
    buf.extend(struct.pack("<f", rec.last_spurt_start_distance))
    buf.append(rec.running_style & 0xFF)
    buf.extend(struct.pack("<i", rec.defeat))
    buf.extend(struct.pack("<f", rec.finish_time_raw))
    assert len(buf) == 31, f"result record must be 31 bytes, got {len(buf)}"
    return bytes(buf)


def encode_scenario(
    horse_num: int,
    frames: list[tuple[float, list[HorseFrame]]],
    results: list[HorseResult],
    distance_diff_max: float = 0.0,
    skill_events: list[SkillEvent] | None = None,
    race_events: list[RaceEvent] | None = None,
) -> bytes:
    assert len(results) == horse_num
    horse_frame_size = _FRAME_HORSE_STRUCT.size
    frame_size = 4 + horse_num * horse_frame_size

    out = bytearray()

    header = struct.pack("<i", HEADER_VERSION)
    out.extend(struct.pack("<i", len(header)))
    out.extend(header)

    out.extend(struct.pack("<fiii", distance_diff_max, horse_num, horse_frame_size, 31))

    out.extend(struct.pack("<i", 0))

    out.extend(struct.pack("<ii", len(frames), frame_size))
    for time_val, horse_frames in frames:
        assert len(horse_frames) == horse_num
        out.extend(struct.pack("<f", time_val))
        for hf in horse_frames:
            encode_horse_frame(out, hf)

    out.extend(struct.pack("<i", 0))

    for rec in results:
        out.extend(encode_result(rec))

    combined_events = [_skill_event_to_race_event(e) for e in (skill_events or [])]
    combined_events += list(race_events or [])
    out.extend(_encode_race_events(combined_events))

    return bytes(out)


def decode_scenario(blob: bytes) -> dict:
    off = 0
    header_len = struct.unpack_from("<i", blob, off)[0]
    version = struct.unpack_from("<i", blob, 4)[0] if header_len >= 4 else HEADER_VERSION
    off += 4 + header_len
    jp = version >= 100000005
    if jp:
        distance_diff_max, horse_num, horse_frame_size = struct.unpack_from("<fii", blob, off)
        off += 12
        horse_result_size = 0
    else:
        distance_diff_max, horse_num, horse_frame_size, horse_result_size = struct.unpack_from("<fiii", blob, off)
        off += 16
    pad1 = struct.unpack_from("<i", blob, off)[0]
    off += 4 + pad1
    frame_count, frame_size = struct.unpack_from("<ii", blob, off)
    off += 8
    frames = []
    for _ in range(frame_count):
        t = struct.unpack_from("<f", blob, off)[0]
        horses = []
        o = off + 4
        for _h in range(horse_num):
            dist, lane_q, speed_q, hp_q, temptation, blockfront = _FRAME_HORSE_STRUCT.unpack_from(blob, o)
            horses.append(HorseFrame(dist, lane_q / LANE_SCALE, speed_q / SPEED_SCALE, hp_q, temptation, blockfront))
            o += horse_frame_size
        frames.append((t, horses))
        off += frame_size
    pad2 = struct.unpack_from("<i", blob, off)[0]
    off += 4 + pad2
    if jp:
        sizes = struct.unpack_from(f"<{horse_num}i", blob, off)
        off += 4 * horse_num
    else:
        sizes = [horse_result_size] * horse_num
    results = []
    for size in sizes:
        rec = blob[off:off + size]
        off += size
        results.append(HorseResult(
            finish_order=struct.unpack_from("<i", rec, 0)[0],
            finish_time=struct.unpack_from("<f", rec, 4)[0],
            finish_diff_time=struct.unpack_from("<f", rec, 8)[0],
            start_delay_time=struct.unpack_from("<f", rec, 12)[0],
            guts_order=rec[16],
            wiz_order=rec[17],
            last_spurt_start_distance=struct.unpack_from("<f", rec, 18)[0],
            running_style=rec[22],
            defeat=struct.unpack_from("<i", rec, 23)[0],
            finish_time_raw=struct.unpack_from("<f", rec, 27)[0],
        ))
    events = []
    if off + 8 <= len(blob):
        _, count = struct.unpack_from("<ii", blob, off)
        off += 8
        for _ in range(count):
            length, t, kind, n = struct.unpack_from("<hfBB", blob, off)
            params = list(struct.unpack_from(f"<{n}i", blob, off + 8))
            events.append({"t": t, "type": kind, "params": params})
            off += 2 + length
    return {
        "distance_diff_max": distance_diff_max,
        "horse_num": horse_num,
        "frame_count": frame_count,
        "frames": frames,
        "results": results,
        "events": events,
    }
