"""
Encoder/decoder for `race_scenario`, the gzip+base64 binary blob the real
client expects in a race response's `data.race_result_info.race_scenario`
for rendering the actual race animation.

This is Cygames' proprietary `Gallop.RaceSimulateData` wire format (see
`RaceSimulateData`/`RaceSimulateHorseFrameData`/`RaceSimulateHorseResultData`
in the IL2CPP dump at
`Career Dump Plugin/il2cppdumper/dump.cs`, around line 390270). The dump only
gives field names/types, not the byte-level layout (IL2CPP method bodies
aren't decompiled), so the layout below was reverse-engineered empirically:
cross-referencing the known field list against real captured blobs (see
`UmaDumpy-main/dumps/*/`), using a partial reference decoder that already
existed in `Icarus-Dev-Build-Private-main/career_bot/dailies.py`
(`parse_race_result_array`, which only decodes the header + results section)
as a starting point and extending it to the frame data.

Validated against real blobs by:
- horse[0]'s starting speed decoding to exactly 3.0 and its first-frame
  acceleration matching the physics engine's known 24.0 start-dash constant
  (see `uma-tools-master/uma-skill-tools/RaceSolver.ts`)
- distance-over-time for every horse increasing monotonically across all
  frames (zero negative deltas) and topping out just past the course's real
  distance (finish-line overshoot, expected for fixed-interval sampling)
- FinishOrder across all horses forming an exact permutation of
  [0, horse_num)
- FinishTime values landing in the expected real-world range for the
  course's distance (matches the *1e4 scaling documented in dailies.py)

Layout (all integers little-endian):
    int32   header_len (always 4)
    bytes   header (int32 Version, currently 100000002)
    float32 distance_diff_max
    int32   horse_num
    int32   horse_frame_size   (bytes per horse per frame, currently 12)
    int32   horse_result_size  (bytes per horse result record, currently 31)
    int32   pad1_len           (unidentified section before frame data;
                                 0 in every real sample seen so far)
    bytes   pad1
    int32   frame_count
    int32   frame_size         (bytes per frame = 4 + horse_num*horse_frame_size)
    bytes   frame data, frame_count frames of frame_size bytes each:
        float32 time
        per horse (horse_frame_size bytes):
            float32 distance
            int16   lane_position (raw = lane * 10000)
            int16   speed         (raw = speed * 100)
            int16   hp            (raw int, not scaled)
            int8    temptation_mode
            int8    block_front_horse_index
    int32   pad2_len           (unidentified section after frame data;
                                 0 in every real sample seen so far)
    bytes   pad2
    bytes   horse_result_size * horse_num bytes of result records, one per
            horse in the SAME index order as the frame data (not the same
            order as frame_order -- see race_horse_data_array[].frame_order
            for the index->horse mapping), each record:
        int32   finish_order (0-indexed)
        float32 finish_time       (seconds; real value = this / 1, already
                                    in seconds -- callers multiply by 1e4 to
                                    match the integer time fields used
                                    elsewhere in the wire protocol)
        float32 finish_diff_time
        float32 start_delay_time
        uint8   guts_order
        uint8   wiz_order
        float32 last_spurt_start_distance (-1.0 if no spurt data)
        uint8   running_style
        int32   defeat
        float32 finish_time_raw

Known gap: real blobs have a further trailing section after the results
(observed ~1.4KB in one 18-horse sample) that isn't accounted for above --
almost certainly skill-activation event markers (`RaceSimulateEventData`:
frameTime, SimulateEventType, int[] param). Omitted here; races should still
animate correctly (position/speed/finish order all come from the sections
above), just without skill-proc visual bursts.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

HEADER_VERSION = 100000002
FRAME_HZ = 15.0  # matches the ~0.0667s spacing seen in every real sample
SPEED_SCALE = 100.0
LANE_SCALE = 10000.0

# The trailing "events" section (skill-activation markers, ~1.4KB after an
# 18-horse race's results) has a real internal structure that isn't fully
# reverse-engineered (see module docstring), and writing nothing/an empty
# marker there isn't enough -- confirmed via the actual client's Player.log:
# a bare `int32 0` there still crashed Gallop.RaceSimulateData's own
# deserializer with an out-of-range read, meaning it expects real content,
# not just a valid-looking empty section. Pragmatic fix: append this exact
# byte sequence, extracted verbatim from a real captured 18-horse race's own
# trailing section (session 20260717_180912), to every encoded blob
# regardless of what race it's for. It won't describe *this* race's actual
# skill procs -- the event timestamps/positions belong to the original
# capture -- but it lets the deserializer complete successfully, which is
# what actually matters (skill-proc visual flair is cosmetic; a crash isn't).
_REAL_EVENTS_TAIL_18HORSE = base64.b64decode(
    "AAAAADAAAAAeAAAAAAADBgAAAADwDgMA/////wAAAAAAAAAAAAAAAB4AAAAAAAMGAgAAADwOAwD/"
    "////AAAAAAAAAAAAAAAAHgAAAAAAAwYCAAAAnxMDAP////8AAAAAAAAAAAAAAAAeAAAAAAADBgMA"
    "AAA8EwMA/////wAAAAAAAAAAAAAAAB4AAAAAAAMGAwAAADwOAwD/////AAAAAAAAAAAAAAAAHgAA"
    "AAAAAwYEAAAAnxMDAP////8AAAAAAAAAAAAAAAAeAAAAAAADBgUAAADsDQMA/////wAAAAAAAAAA"
    "AAAAAB4AAAAAAAMGBwAAAEwNAwD/////AAAAAAAAAAAAAAAAHgAAAAAAAwYHAAAA4g0DAP////8A"
    "AAAAAAAAAAAAAAAeAAAAAAADBggAAACfEwMA/////wAAAAAAAAAAAAAAAB4AAAAAAAMGCQAAAJ8T"
    "AwD/////AAAAAAAAAAAAAAAAHgAAAAAAAwYJAAAAKA4DAP////8AAAAAAAAAAAAAAAAeAAAAAAAD"
    "BgoAAADwDgMA/////wAAAAAAAAAAAAAAAB4AAAAAAAMGCgAAAEwNAwD/////AAAAAAAAAAAAAAAA"
    "HgAAAAAAAwYOAAAA7A0DAP////8AAAAAAAAAAAAAAAAeAAAAAAADBg8AAAAoDgMA/////wAAAAAA"
    "AAAAAAAAAB4AAAAAAAMGEAAAAH4NAwD/////AAAAAAAAAAAAAAAAHgAAAAAAAwYQAAAA4g0DAP//"
    "//8AAAAAAAAAAAAAAAAeAAAAAAADBhAAAACfEwMA/////wAAAAAAAAAAAAAAAB4AAAAAAAMGEAAA"
    "AFQPAwD/////AAAAAAAAAAAAAAAAHgAAAAAAAwYRAAAAxA0DAP////8AAAAAAAAAAAAAAAAeAKD4"
    "oUADBg4AAAB3EwMAAHcBAAAAAAAAQAAAAAAAAB4ACKXdQAMGDAAAAHQSAwAAdwEAAAAAAAAQAAAA"
    "AAAAHgD2Q4ZBAwYIAAAAOBIDAAB3AQAAAAAAAAEAAAAAAAAKACQxjUEEARAAAAAKACQxjUEEAQ8A"
    "AAAeAKDR+EEDBgYAAAANDwMA/+AAAAAAAABAAAAAAAAAAB4ABAT8QQMGDwAAADgSAwAAdwEAAAAA"
    "AACAAAAAAAAAHgAw7wRCAwYPAAAADg8DAP/gAAAAAAAAAIAAAAAAAAAeAPn+CUIDBgUAAADOEgMA"
    "AAAAAAAAAAAgAAAAAAAAAB4AicgWQgMGBgAAAHwPAwAAAAAAAAAAAEAAAAAAAAAAHgCIHRhCAwYM"
    "AAAAahIDAAB3AQAAAAAAABAAAAAAAAAeABE6LkIDBgUAAADYEgMAAHcBAAAAAAAgAAAAAAAAAB4A"
    "w+xRQgMGAQAAAL0TAwAAdwEAAAAAAAIAAAAAAAAAHgD2wqpCAwYNAAAAJhADAAAAAAAAAAAAACAA"
    "AAAAAAAeAJOwr0IDBhAAAABeDwMAAHcBAAAAAAAAAAEAAAAAAB4Ads+7QgMGCwAAAL0TAwAAdwEA"
    "AAAAAAAIAAAAAAAAHgDm3/1CAwYGAAAAiw4DAP8rAQAAAAAAQAAAAAAAAAAeAKDMA0MDBhEAAABj"
    "EwMAAAAAAAAAAAAAAAIAAAAAAB4Ax/8DQwMGAAAAAGMTAwAAAAAAAAAAAAEAAAAAAAAAHgD+hglD"
    "AwYGAAAAN4cBAABxAgAAAAAAQAAAAAAAAAAeAFjsDUMDBhEAAACkDwMAAHcBAAAAAAAAAAIAAAAA"
    "AAoArNgYQwUBCgAAAAoArNgYQwUBDQAAAAoAxvoYQwUBBAAAAAoAxvoYQwUBDgAAAAoAIXIZQwUB"
    "DAAAAAoAIXIZQwUBEQAAAA=="
)

# Same idea, extracted from a real 10-horse practice race (session
# 20260717_180912's practice_race sibling capture). horse_num varies
# per real race (5/9/12/14/15/16/17/18, from master.mdb's race.entry_num --
# practice races are not always full 18-horse fields), and the events
# section's records reference horse indices directly, so a mismatched
# horse_num template risks the client indexing into a race_horse_data_array
# that's smaller than what the borrowed events reference. Only have real
# templates for two horse counts; _events_tail_for() below picks whichever
# is closer for anything else, which isn't exact but keeps the reference
# indices roughly in range.
_REAL_EVENTS_TAIL_10HORSE = base64.b64decode(
    "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"
    "AwCAuwAAAAAAAIAAAAAAAAAA"
)


def _events_tail_for(horse_num: int) -> bytes:
    templates = {10: _REAL_EVENTS_TAIL_10HORSE, 18: _REAL_EVENTS_TAIL_18HORSE}
    if horse_num in templates:
        return templates[horse_num]
    return min(templates.items(), key=lambda kv: abs(kv[0] - horse_num))[1]


@dataclass
class SkillEvent:
    horse_index: int
    skill_id: int
    t: float
    duration: float
    is_unique: bool = False


# Ground truth, from decompiling the real client's own methods (via a
# process memory dump of GameAssembly.dll -- the on-disk file has its code
# sections packed/encrypted, which is why earlier attempts against it
# produced nothing but zero-filled garbage; Ghidra's own Exception
# Directory parsing also independently fails on the packed file, which is
# what forced this whole detour). Every earlier "32-byte fixed record"
# understanding was subtly wrong -- see git history for the abandoned
# theories -- but was *self-consistently* wrong in a way that happened to
# survive byte-for-byte round-trip checks against real captures, which is
# why it took disassembly to actually find the bug: the real per-event
# format, confirmed directly from
# Gallop.RaceSimulateEventData._Deserialize_Ver20200406_OrNewer and the
# calling loop in Gallop.RaceSimulateData.Deserialize_Ver20200303_OrNewer,
# is a length-prefixed record:
#   int16   record_length   (byte count of everything below, NOT including
#                             this field itself -- i.e. 6 + paramCount*4)
#   float32 frameTime       (activation time, seconds, matches accumulatetime)
#   uint8   type            (SimulateEventType; 3 = Skill)
#   uint8   paramCount      (element count of the field below -- read
#                             directly as a single byte and used as an
#                             array-allocation size, confirmed in the
#                             decompiled deserializer)
#   int32[paramCount] param (every real Skill record seen has paramCount=6:
#                             [horse_index, skill_id, duration*10000,
#                             is_unique, 0, 0] -- the last two are always 0
#                             in every real sample; true meaning unknown,
#                             but 0 round-trips correctly, so used here too)
# The previous "32-byte struct with two leading unknown ints and a constant
# int16 marker=30" was this SAME layout misread 8 bytes out of phase: what
# looked like "unknown0/unknown1" was actually the *previous* record's own
# trailing param[4]/param[5] (both usually 0, hence looking like constant
# unrelated fields), and the "constant marker=30" was actually the length
# field's real value for the common 6-param case (2+4+1+1+24-2=30) -- not a
# validation sentinel at all. That 8-byte misalignment is also exactly why
# synthesized records reliably crashed even when whitelisted/format-correct
# by the old (wrong) model: the deserializer's length-prefix read landed on
# 8 bytes of unrelated data instead of the real length.
_SKILL_EVENT_PARAM_COUNT = 6
_SKILL_EVENT_STRUCT = struct.Struct("<hfBBiiiiii")  # length,time,type,paramCount,6x param


@dataclass
class RaceEvent:
    """A general RaceSimulateEventData record: SimulateEventType `type_id`
    with `params` (int32 each). Confirmed from real captured race_scenario
    blobs (decoded byte-for-byte against this same length-prefixed format):
    every real race carries THREE event types, not just Skill --

        type=3 (Skill):  paramCount=6, [horse_index, skill_id, duration*10000,
                          is_unique, 0, 0] -- see SkillEvent/skill_events below.
        type=4 (Kakari/Rushing START): paramCount=1, [horse_index]. Always a
                          small handful, early in the race, near-simultaneous
                          across the affected horses.
        type=5 (Finished): paramCount=1, [horse_index]. ONE per horse,
                          clustered at each horse's own finish time --
                          present in 100% of real captures checked. This
                          server sent NEITHER type before, only type=3.

    isUnique isn't a param slot here (only Skill records have one, folded
    into their own params tuple) -- kept generic so type=4/5 don't need a
    fake extra field."""
    t: float
    type_id: int
    params: tuple


def _encode_race_events(events: list[RaceEvent]) -> bytes:
    sorted_events = sorted(events, key=lambda e: e.t)
    out = bytearray()
    out += struct.pack("<i", 0)  # leading reserved/pad field, 0 in every real sample
    out += struct.pack("<i", len(sorted_events))
    for e in sorted_events:
        param_count = len(e.params)
        record_length = 4 + 1 + 1 + param_count * 4  # everything after the length prefix itself
        out += struct.pack(f"<hfBB{param_count}i", record_length, e.t, e.type_id, param_count, *e.params)
    return bytes(out)


def _skill_event_to_race_event(e: SkillEvent) -> RaceEvent:
    return RaceEvent(t=e.t, type_id=3, params=(
        e.horse_index, e.skill_id, round(e.duration * 10000),
        1 if e.is_unique else 0, 0, 0,
    ))


_FRAME_HORSE_STRUCT = struct.Struct("<fhhhbb")  # distance, lane, speed, hp, temptation, blockfront
_RESULT_STRUCT_HEAD = struct.Struct("<ifff")  # order, time, diff, delay
_RESULT_STRUCT_TAIL = struct.Struct("<fBf")  # last_spurt, running_style(+pad via manual byte), finish_time_raw


@dataclass
class HorseFrame:
    distance: float
    lane_position: float
    speed: float
    hp: int
    temptation_mode: int = 0
    block_front_horse_index: int = -1


@dataclass
class HorseResult:
    finish_order: int  # 0-indexed
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
    lane_q = max(-32768, min(32767, round(hf.lane_position * LANE_SCALE)))
    speed_q = max(-32768, min(32767, round(hf.speed * SPEED_SCALE)))
    hp_q = max(-32768, min(32767, round(hf.hp)))
    buf.extend(_FRAME_HORSE_STRUCT.pack(
        hf.distance, lane_q, speed_q, hp_q, hf.temptation_mode, hf.block_front_horse_index
    ))


def encode_result(rec: HorseResult) -> bytes:
    buf = bytearray()
    buf.extend(struct.pack("<i", rec.finish_order))
    buf.extend(struct.pack("<f", rec.finish_time))
    buf.extend(struct.pack("<f", rec.finish_diff_time))
    buf.extend(struct.pack("<f", rec.start_delay_time))
    buf.append(rec.guts_order & 0xFF)
    buf.append(rec.wiz_order & 0xFF)
    buf.extend(struct.pack("<f", rec.last_spurt_start_distance))
    buf.append(rec.running_style & 0xFF)
    buf.extend(struct.pack("<i", rec.defeat))
    buf.extend(struct.pack("<f", rec.finish_time_raw))
    assert len(buf) == 31, f"result record must be 31 bytes, got {len(buf)}"
    return bytes(buf)


def encode_scenario(
    horse_num: int,
    frames: list[tuple[float, list[HorseFrame]]],  # (time, [HorseFrame per horse])
    results: list[HorseResult],  # indexed same as frames' horse order
    distance_diff_max: float = 0.0,
    skill_events: list[SkillEvent] | None = None,
    race_events: list[RaceEvent] | None = None,
) -> bytes:
    """Build the raw (pre-gzip) RaceSimulateData binary blob."""
    assert len(results) == horse_num
    horse_frame_size = _FRAME_HORSE_STRUCT.size
    frame_size = 4 + horse_num * horse_frame_size

    out = bytearray()

    # header: length-prefixed int32 Version
    header = struct.pack("<i", HEADER_VERSION)
    out.extend(struct.pack("<i", len(header)))
    out.extend(header)

    # metadata block
    out.extend(struct.pack("<fiii", distance_diff_max, horse_num, horse_frame_size, 31))

    # pad1 (unidentified, empty)
    out.extend(struct.pack("<i", 0))

    # frame data
    out.extend(struct.pack("<ii", len(frames), frame_size))
    for time_val, horse_frames in frames:
        assert len(horse_frames) == horse_num
        out.extend(struct.pack("<f", time_val))
        for hf in horse_frames:
            encode_horse_frame(out, hf)

    # pad2 (unidentified, empty -- skill event markers would go here)
    out.extend(struct.pack("<i", 0))

    # results
    for rec in results:
        out.extend(encode_result(rec))

    # events (skill-activation markers): a bare empty marker here was
    # confirmed (via the client's own Player.log) to crash
    # Gallop.RaceSimulateData's deserializer with an out-of-range read, so
    # this section is never left empty. Re-enabled with the ground-truth
    # length-prefixed format decompiled from the real client -- see
    # _encode_skill_events' docstring for the full record layout and why
    # every earlier attempt crashed (an 8-byte misalignment in the assumed
    # record structure, invisible to byte-for-byte self-consistency checks).
    combined_events = [_skill_event_to_race_event(e) for e in (skill_events or [])]
    combined_events += list(race_events or [])
    if combined_events:
        out.extend(_encode_race_events(combined_events))
    else:
        out.extend(_events_tail_for(horse_num))

    return bytes(out)


def decode_scenario(blob: bytes) -> dict:
    """Reference decoder, used only for round-trip validation in tests."""
    off = 0
    header_len = struct.unpack_from("<i", blob, off)[0]
    off += 4 + header_len
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
    results = []
    for i in range(horse_num):
        base = off + i * horse_result_size
        rec = blob[base:base + horse_result_size]
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
    return {
        "distance_diff_max": distance_diff_max,
        "horse_num": horse_num,
        "frame_count": frame_count,
        "frames": frames,
        "results": results,
    }
