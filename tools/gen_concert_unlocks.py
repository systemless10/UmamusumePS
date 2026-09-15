"""Generate server/app/data/concert_unlocks.json from the Concert Theater wiki.

Structured, vendored output rather than runtime HTML parsing: the wiki page is
a snapshot in docs/ and re-scraping it on every request would be both slow and
fragile. Regenerate by re-running this script when the page is refreshed.
"""
import io, json, os, re, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = r"C:\Users\Systemless\Documents\UmaPS"
sys.path.insert(0, os.path.join(ROOT, "server"))
from app import master_data

HTML = os.path.join(ROOT, "docs", "Game_Concert Theater - Umamusume Wiki.html")
OUT = os.path.join(ROOT, "server", "app", "data", "concert_unlocks.json")

html = io.open(HTML, encoding="utf-8", errors="replace").read()
text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S | re.I)
text = re.sub(r"<[^>]+>", "\n", text)
text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
lines = [l.strip() for l in text.split("\n") if l.strip()]

# Global song set = whatever live_data carries; JP-only songs never appear there.
title_to_ids = {}
for r in master_data.query("SELECT music_id FROM live_data"):
    t = master_data.query_one(
        "SELECT text FROM text_data WHERE category=16 AND [index]=?", (r["music_id"],))
    if t and t["text"]:
        title_to_ids.setdefault(t["text"].strip(), []).append(r["music_id"])

# text_data 252 distinguishes the two Girls' Legend U arrangements.
mid_252 = {}
for r in master_data.query("SELECT [index], text FROM text_data WHERE category=252"):
    mid_252[r["index"]] = r["text"]

RACE_NAMES = {r["text"].strip() for r in master_data.query(
    "SELECT DISTINCT text FROM text_data WHERE category IN (28,29)") if r["text"]}


def classify(acq_lines):
    """(kind, params, note) for a How-to-Acquire block kept as separate lines."""
    joined = " ".join(acq_lines)
    flat = re.sub(r"\s+", " ", joined).strip().rstrip(".")

    m = re.match(r"Watch Episode (\d+) of Chapter (\d+) of Main Story - Act (\d+)", flat)
    if m:
        return "main_story_episode", {"act": int(m.group(3)), "chapter": int(m.group(2)),
                                      "episode": int(m.group(1))}, None
    m = re.match(r"Watch Episode (\d+) of the Main Story - Act (\d+) finale", flat)
    if m:
        return "main_story_finale", {"act": int(m.group(2)), "episode": int(m.group(1))}, None
    m = re.match(r"Watch Episode (\d+) of the Story Event (.+)", flat)
    if m:
        return "story_event_episode", {"episode": int(m.group(1)),
                                       "event": m.group(2).strip()}, "story events untracked"
    m = re.match(r"Watch Episode (\d+) of the (.+?) Story", flat)
    if m:
        return "extra_story_episode", {"episode": int(m.group(1)),
                                       "story": m.group(2).strip()}, "extra stories untracked"
    if flat.startswith("Win one of the following races:"):
        races = [l.strip() for l in acq_lines[1:] if l.strip() in RACE_NAMES]
        return "win_any_race", {"races": races}, (None if races else "no race names matched")
    if re.match(r"Get first place in a race ranked (\w+) or lower", flat):
        g = re.match(r"Get first place in a race ranked (\w+) or lower", flat).group(1)
        return "win_grade_at_most", {"grade": g}, None
    if "URA Finale Finals" in flat:
        return "ura_finals", {}, None
    if "Unity Cup finals" in flat:
        return "unity_cup_finals", {}, None
    if "Twinkle Star Climax" in flat:
        return "ts_climax", {}, None
    if re.search(r"Hold a Special Grand Concert", flat, re.I):
        return "special_grand_concert", {}, None
    if re.search(r"Hold a regular Grand Concert", flat, re.I):
        return "grand_concert", {}, None
    if "Unlocked by default" in flat or flat.startswith("Distributed"):
        return "default", {}, None
    return "unknown", {}, "unrecognised acquisition text"


entries = []
for i, l in enumerate(lines):
    if l != "How to Acquire":
        continue
    title = None
    for j in range(i - 1, max(0, i - 40), -1):
        if lines[j] == "Comment":
            title = lines[j - 1]
            break
    acq = []
    for j in range(i + 1, min(len(lines), i + 14)):
        if lines[j] in ("Singers", "Comment"):
            break
        acq.append(lines[j])
    ids = title_to_ids.get((title or "").strip(), [])
    if not ids:
        continue  # JP-only / not released on Global -- never granted here
    kind, params, note = classify(acq)
    # Girls' Legend U ships as two arrangements; text_data 252 says which is
    # which ("Put on a Grand Concert" vs "...a special Grand Concert").
    for mid in ids:
        k, p = kind, params
        hint = mid_252.get(mid, "")
        if "special Grand Concert" in hint:
            k, p = "special_grand_concert", {}
        elif "Put on a Grand Concert" in hint:
            k, p = "grand_concert", {}
        entries.append({"music_id": mid, "title": title, "kind": k, "params": p,
                        "source": re.sub(r"\s+", " ", " ".join(acq)).strip(),
                        "note": note})

entries.sort(key=lambda e: e["music_id"])
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump({"_generated_by": "tools/gen_concert_unlocks.py from "
                            "docs/Game_Concert Theater - Umamusume Wiki.html",
           "songs": entries},
          io.open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

import collections
print("wrote %d Global song entries to %s" % (len(entries), OUT))
print(collections.Counter(e["kind"] for e in entries))
print()
for e in entries:
    print("%5d %-36s %-22s %s" % (e["music_id"], (e["title"] or "")[:36], e["kind"],
                                  json.dumps(e["params"], ensure_ascii=False)[:60]))
