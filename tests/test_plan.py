import hashlib
import io
import json
import os
import sys
import zipfile

import mido
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guitar_route209.main import app  # noqa: E402
from test_compare import make_midi, notes_track  # noqa: E402

client = TestClient(app)
EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "examples")


def plan(file_bytes, **form):
    data = {
        "track": form.get("track", 1),
        "channel": form.get("channel", 0),
    }
    if "open_strings" in form:
        data["open_strings"] = (form["open_strings"]
                                if isinstance(form["open_strings"], str)
                                else json.dumps(form["open_strings"]))
    files = {"file": ("song.mid", file_bytes, "audio/midi")}
    return client.post("/plan", files=files, data=data)


def example(name):
    with open(os.path.join(EXAMPLES, name), "rb") as fh:
        return fh.read()


def unzip(r):
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    plan_json = json.loads(zf.read("plan.json"))
    annotated = zf.read("annotated.mid")
    return plan_json, annotated


def test_plan_example_song():
    src = example("plan_song.mid")
    r = plan(src)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    plan_json, annotated = unzip(r)
    assert plan_json["source_sha256"] == hashlib.sha256(src).hexdigest()
    assert plan_json["note_count"] == 12
    assert plan_json["open_strings"] == [64, 59, 55, 50, 45, 40]
    assert plan_json["total_position_movement"] >= 0
    assert plan_json["string_changes"] >= 0
    opens = plan_json["open_strings"]
    for i, item in enumerate(plan_json["notes"]):
        assert item["index"] == i
        assert item["fret"] == item["pitch"] - opens[item["string"] - 1]
        assert 0 <= item["fret"] <= 20
        assert 1 <= item["position"] <= 17
        if item["fret"] > 0:
            assert item["position"] <= item["fret"] <= item["position"] + 3


def test_annotated_midi_inserts_text_and_preserves_events():
    src = example("plan_song.mid")
    r = plan(src)
    assert r.status_code == 200
    plan_json, annotated = unzip(r)
    original = mido.MidiFile(file=io.BytesIO(src))
    out = mido.MidiFile(file=io.BytesIO(annotated))
    assert out.type == original.type
    assert out.ticks_per_beat == original.ticks_per_beat
    assert len(out.tracks) == len(original.tracks)
    # Untouched tracks are byte-identical in content.
    for k in (0,):
        assert [str(m) for m in out.tracks[k]] == \
            [str(m) for m in original.tracks[k]]
    track = out.tracks[1]
    texts = [m for m in track if m.is_meta and m.type == "text"]
    assert len(texts) == 12
    for i, m in enumerate(texts):
        item = plan_json["notes"][i]
        assert m.text == (f"plan: index={i} "
                          f"string={item['string']} "
                          f"fret={item['fret']} "
                          f"position={item['position']}")
    # Every text sits on the same tick, right before its note_on.
    tick = 0
    abs_events = []
    for msg in track:
        tick += msg.time
        abs_events.append((tick, msg))
    ons = [(t, m) for t, m in abs_events
           if m.type == "note_on" and m.velocity > 0]
    for i, (t, on) in enumerate(ons):
        idx = abs_events.index((t, on))
        prev_tick, prev_msg = abs_events[idx - 1]
        assert prev_tick == t
        assert prev_msg.is_meta and prev_msg.type == "text"
        assert f"index={i}" in prev_msg.text
    # Original non-text events keep content, absolute tick and order.
    def stripped(trk, skip_text):
        tick = 0
        kept = []
        for msg in trk:
            tick += msg.time
            if skip_text and msg.is_meta and msg.type == "text":
                continue
            kept.append((tick, msg.copy(time=0)))
        return [(t, str(m)) for t, m in kept]

    assert stripped(track, True) == stripped(original.tracks[1], False)


def test_open_string_note_prefers_lexicographically_smallest():
    # A single open-string note: any position fits, position 1 wins.
    song = make_midi(tracks=[notes_track([64])])
    r = plan(song)
    assert r.status_code == 200
    plan_json, _ = unzip(r)
    item = plan_json["notes"][0]
    assert (item["string"], item["fret"], item["position"]) == (1, 0, 1)
    assert plan_json["total_position_movement"] == 0


def _timed_midi(notes, tempo=480000, ppqn=480):
    # notes: list of (pitch, start_tick, end_tick); 1 tick == 1 ms.
    events = []
    prev = 0
    for pitch, start, end in sorted(notes, key=lambda n: n[1]):
        events.append(mido.Message("note_on", note=pitch, velocity=80,
                                   time=start - prev, channel=0))
        events.append(mido.Message("note_off", note=pitch, velocity=0,
                                   time=end - start, channel=0))
        prev = end
    return make_midi(ppqn=ppqn, tempos=[(0, tempo)], tracks=[events])


def test_shift_equality_is_reachable():
    # 41 forces string 6 fret 1 (position 1); 84 forces string 1 fret 20
    # (position 17). 16 frets * 20 ms = 320 ms gap, exactly available.
    song = _timed_midi([(41, 0, 240), (84, 560, 800)])
    r = plan(song)
    assert r.status_code == 200
    plan_json, _ = unzip(r)
    assert plan_json["total_position_movement"] == 16
    assert [(n["string"], n["fret"], n["position"])
            for n in plan_json["notes"]] == [(6, 1, 1), (1, 20, 17)]


def test_shift_one_tick_short_rejected_and_located():
    song = _timed_midi([(41, 0, 240), (84, 559, 800)])  # 319 ms < 320 ms
    r = plan(song)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["note"] == 1
    assert "no reachable position" in d["message"]


def test_open_string_previous_note_allows_early_shift():
    # Middle note is an open string: the shift to position 17 may start
    # at its onset, so 320 ms is available even though the gap after its
    # end is only 80 ms. 1 tick == 1 ms.
    song = _timed_midi([(41, 0, 100), (64, 100, 340), (84, 420, 520)])
    r = plan(song)
    assert r.status_code == 200
    plan_json, _ = unzip(r)
    assert plan_json["notes"][1]["fret"] == 0
    assert plan_json["total_position_movement"] == 16


def test_pressed_previous_note_blocks_early_shift():
    # Same layout but the middle note is pressed (62 on string 2 fret 3,
    # position 1): only the 80 ms gap after its end is usable, which is
    # short of the 320 ms needed, so note 2 is unreachable.
    song = _timed_midi([(41, 0, 100), (62, 100, 340), (84, 420, 520)])
    r = plan(song)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["note"] == 2
    assert "no reachable position" in d["message"]


def test_no_candidate_note_rejected_and_located():
    song = make_midi(tracks=[notes_track([60, 100, 62])])
    r = plan(song)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["note"] == 1
    assert "no playable" in d["message"]


def test_global_plan_not_greedy():
    # 60 then 41 then 60 with no gaps: a per-note greedy pick of the
    # lexicographically smallest state for note 0 ((2,1,1)) would force
    # 1 -> 1 -> 1 movement... but string 6 fret 1 also fits note 0/2 at
    # higher cost per note; the global optimum keeps movement 0 and must
    # be unique. Zero gap means only shared positions survive.
    song = _timed_midi([(60, 0, 100), (41, 100, 200), (60, 200, 300)])
    r = plan(song)
    assert r.status_code == 200
    plan_json, _ = unzip(r)
    triples = [(n["string"], n["fret"], n["position"])
               for n in plan_json["notes"]]
    assert triples == [(2, 1, 1), (6, 1, 1), (2, 1, 1)]
    assert plan_json["total_position_movement"] == 0
    assert plan_json["string_changes"] == 2


def test_open_strings_override():
    song = make_midi(tracks=[notes_track([64])])
    r = plan(song, open_strings=[67, 62, 57, 50, 45, 40])
    assert r.status_code == 200
    plan_json, _ = unzip(r)
    assert plan_json["open_strings"] == [67, 62, 57, 50, 45, 40]
    item = plan_json["notes"][0]
    # 64 is no longer open on string 1; best is string 4 fret 14? No:
    # lexicographically smallest triple wins: string 2 fret 2 position 1.
    assert (item["string"], item["fret"], item["position"]) == (2, 2, 1)


def test_open_strings_invalid():
    song = make_midi(tracks=[notes_track([64])])
    assert plan(song, open_strings=[64, 59]).status_code == 422
    assert plan(song, open_strings=[64, 59, 55, 50, 45, 128]
                ).status_code == 422
    assert plan(song, open_strings="nope").status_code == 422


def test_long_text_smf_accepted():
    # A legal SMF carrying a >1 MiB text meta event must not be falsely
    # rejected by the parser message-length cap.
    events = [mido.MetaMessage("text", text="x" * 1_500_000, time=0)]
    events += notes_track([60, 62])
    song = make_midi(tracks=[events])
    r = plan(song)
    assert r.status_code == 200


def test_missing_file_and_bad_selector():
    r = client.post("/plan", data={"track": 1, "channel": 0})
    assert r.status_code == 422
    song = make_midi(tracks=[notes_track([60])])
    assert plan(song, track=9).status_code == 422
    assert plan(song, channel=16).status_code == 422
