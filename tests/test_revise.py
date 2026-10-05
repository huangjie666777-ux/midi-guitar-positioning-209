import hashlib
import io
import json
import os
import sys
import zipfile

import mido
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guitar_route209.main import app  # noqa: E402
from test_compare import REF, make_midi, notes_track  # noqa: E402

client = TestClient(app)
EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "examples")


def revise(ref_bytes, perf_bytes, actions, **form):
    data = {
        "ref_track": form.get("ref_track", 1),
        "ref_channel": form.get("ref_channel", 0),
        "perf_track": form.get("perf_track", 1),
        "perf_channel": form.get("perf_channel", 0),
        "actions": actions if isinstance(actions, str) else json.dumps(
            actions),
    }
    files = {
        "reference": ("ref.mid", ref_bytes, "audio/midi"),
        "performance": ("perf.mid", perf_bytes, "audio/midi"),
    }
    return client.post("/revise", files=files, data=data)


def example(name):
    with open(os.path.join(EXAMPLES, name), "rb") as fh:
        return fh.read()


def unzip(resp):
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    mid = mido.MidiFile(file=io.BytesIO(zf.read("revised.mid")))
    audit = json.loads(zf.read("audit.json"))
    return mid, audit


def track_notes(mid, track=1, channel=0):
    out = []
    tick = 0
    open_on = {}
    for msg in mid.tracks[track]:
        tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and \
                msg.channel == channel:
            open_on[msg.note] = (tick, msg.velocity)
        elif msg.type in ("note_on", "note_off") and \
                msg.channel == channel and msg.note in open_on:
            start, vel = open_on.pop(msg.note)
            out.append((msg.note, start, tick, vel))
    return sorted(out, key=lambda n: n[1])


def test_fix_pitch_and_delete_extra():
    ref_b = example("reference.mid")
    perf_b = example("performance.mid")
    actions = [
        {"op": "fix_pitch", "perf_index": 2},
        {"op": "delete_extra", "perf_index": 5},
    ]
    r = revise(ref_b, perf_b, actions)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    mid, audit = unzip(r)
    # Type/PPQN/track count preserved.
    assert mid.type == 1
    assert mid.ticks_per_beat == 480
    assert len(mid.tracks) == 2
    notes = track_notes(mid)
    assert [n[0] for n in notes] == [60, 62, 64, 65, 67]
    # Velocity and ticks preserved for the fixed note.
    assert notes[2][3] == 90
    assert (notes[2][1], notes[2][2]) == (1008, 1208)
    # Audit records sources and before/after.
    assert audit["reference_sha256"] == hashlib.sha256(ref_b).hexdigest()
    assert audit["performance_sha256"] == hashlib.sha256(perf_b).hexdigest()
    revised_bytes = zipfile.ZipFile(io.BytesIO(r.content)).read("revised.mid")
    assert audit["revised_sha256"] == hashlib.sha256(
        revised_bytes).hexdigest()
    fix = audit["actions"][0]
    assert fix["op"] == "fix_pitch"
    assert fix["pitch_before"] == 63
    assert fix["pitch_after"] == 64
    assert (fix["start_tick"], fix["end_tick"]) == (1008, 1208)
    delete = audit["actions"][1]
    assert delete["op"] == "delete_extra"
    assert delete["pitch_before"] == 70
    # Revised file now compares clean against the reference.
    r2 = client.post("/compare", files={
        "reference": ("ref.mid", ref_b, "audio/midi"),
        "performance": ("p.mid", revised_bytes, "audio/midi"),
    }, data={"ref_track": 1, "ref_channel": 0,
             "perf_track": 1, "perf_channel": 0})
    body = r2.json()
    assert body["errors"]["wrong_pitch"]["count"] == 0
    assert body["errors"]["extra"]["count"] == 0


def test_insert_missed_half_up_rounding():
    # Reference: 60 at 0-240 ticks, 62 at 480-720 (default tempo).
    ref = make_midi(tracks=[notes_track([60, 62])])
    # Performance plays only the 60; tempo 250000 us/qn so time runs 2x
    # fast. Missed 62 spans 500..750 ms absolute.
    perf = make_midi(tracks=[notes_track([60])], tempos=[(0, 250000)])
    r = revise(ref, perf, [{"op": "insert_missed", "ref_index": 1}])
    assert r.status_code == 200
    mid, audit = unzip(r)
    notes = track_notes(mid)
    # 500 ms at 250000 us/qn -> 960 ticks; 750 ms -> 1440 ticks exactly.
    ins = [n for n in notes if n[0] == 62][0]
    assert (ins[1], ins[2]) == (960, 1440)
    assert ins[3] == 80
    entry = audit["actions"][0]
    assert entry["op"] == "insert_missed"
    assert entry["pitch_after"] == 62
    assert (entry["start_tick"], entry["end_tick"]) == (960, 1440)


def test_insert_missed_odd_tick_rounds_half_up():
    # Ref tempo 500000 us/qn: 1 tick = 3125/3 us. Perf tempo 1000000:
    # 1 tick = 6250/3 us, so ref note 62 (ticks 1..3) maps to exactly
    # 0.5 and 1.5 perf ticks; half-up rounding gives 1 and 2.
    ref = make_midi(tracks=[notes_track([60], dur=1, gap=0) +
                            notes_track([62], dur=2, gap=0)])
    perf = make_midi(tracks=[notes_track([60], dur=1, gap=0)],
                     tempos=[(0, 1000000)])
    r = revise(ref, perf, [{"op": "insert_missed", "ref_index": 1}])
    assert r.status_code == 200
    mid, audit = unzip(r)
    entry = audit["actions"][0]
    assert entry["start_tick"] == 1
    assert entry["end_tick"] == 2


def test_insert_zero_length_after_rounding_rejected():
    # Reference note so short that both ends round to the same perf tick.
    # Perf tempo 2000000 us/qn: 1 perf tick = 4 ref ticks; a 1-tick ref
    # note at tick 240 maps to 60.0..60.25 perf ticks -> both round to 60.
    ref = make_midi(tracks=[notes_track([60], dur=240, gap=240) +
                            notes_track([62], dur=1, gap=0)])
    perf = make_midi(tracks=[notes_track([60])], tempos=[(0, 2000000)])
    r = revise(ref, perf, [{"op": "insert_missed", "ref_index": 1}])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["action"] == 0
    assert "zero-length" in d["message"]


def test_reject_nonexistent_action():
    r = revise(REF, REF, [{"op": "fix_pitch", "perf_index": 0}])
    assert r.status_code == 422
    assert "no wrong-pitch error" in r.json()["detail"]["message"]


def test_reject_duplicate_and_conflicting():
    ref_b = example("reference.mid")
    perf_b = example("performance.mid")
    dup = [{"op": "delete_extra", "perf_index": 5},
           {"op": "delete_extra", "perf_index": 5}]
    r = revise(ref_b, perf_b, dup)
    assert r.status_code == 422
    assert r.json()["detail"]["action"] == 1
    conflict = [{"op": "fix_pitch", "perf_index": 5},
                {"op": "delete_extra", "perf_index": 5}]
    # perf 5 is extra, not wrong-pitch: nonexistent takes precedence.
    r = revise(ref_b, perf_b, conflict)
    assert r.status_code == 422
    conflict2 = [{"op": "delete_extra", "perf_index": 5},
                 {"op": "fix_pitch", "perf_index": 5}]
    r = revise(ref_b, perf_b, conflict2)
    assert r.status_code == 422


def test_reject_resulting_overlap_locates_action():
    # Reference second note overlaps the performed first note in time.
    ref = make_midi(tracks=[notes_track([60, 62])])
    perf = make_midi(tracks=[notes_track([60], dur=960, gap=0)])
    r = revise(ref, perf, [{"op": "insert_missed", "ref_index": 1}])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["action"] == 0
    assert "overlap" in d["message"]


def test_reject_bad_actions_payload():
    assert revise(REF, REF, []).status_code == 422
    assert revise(REF, REF, "not json").status_code == 422
    assert revise(REF, REF, [{"op": "nope", "perf_index": 0}]
                  ).status_code == 422
    assert revise(REF, REF, [{"op": "fix_pitch", "perf_index": -1}]
                  ).status_code == 422


def test_overlap_insert_before_existing_keeps_action_location():
    # The inserted note starts before the existing one it overlaps, so
    # the offending action sits on the earlier interval and must still
    # be located in the rejection.
    ref = make_midi(tracks=[notes_track([60, 62, 67], dur=960, gap=480)])
    perf_events = [
        mido.Message("note_on", note=62, velocity=80, time=480,
                     channel=0),
        mido.Message("note_off", note=62, velocity=0, time=240,
                     channel=0),
        mido.Message("note_on", note=67, velocity=80, time=240,
                     channel=0),
        mido.Message("note_off", note=67, velocity=0, time=240,
                     channel=0),
    ]
    perf = make_midi(tracks=[perf_events])
    r = revise(ref, perf, [{"op": "insert_missed", "ref_index": 0}])
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["action"] == 0
    assert "overlap" in d["message"]


def test_long_actions_text_within_2mib_accepted():
    # A legal long text field (>1 MiB, <2 MiB) must not be falsely
    # rejected by the multipart layer; padded JSON stays valid.
    actions = json.dumps([{"op": "delete_extra", "perf_index": 5}])
    pad = 1024 * 1024 + 100
    padded = actions[:-1] + " " * pad + "]"
    r = revise(example("reference.mid"), example("performance.mid"), padded)
    assert r.status_code == 200


def test_insert_extends_track_end():
    # Performance ends early; the inserted note passes end_of_track.
    ref = make_midi(tracks=[notes_track([60, 62])])
    perf = make_midi(tracks=[notes_track([60], dur=100, gap=0)])
    r = revise(ref, perf, [{"op": "insert_missed", "ref_index": 1}])
    assert r.status_code == 200
    mid, _ = unzip(r)
    track = mid.tracks[1]
    tick = 0
    last_note_tick = 0
    eot_tick = None
    for msg in track:
        tick += msg.time
        if msg.type == "note_off" or (msg.type == "note_on"
                                      and msg.velocity == 0):
            last_note_tick = tick
        if msg.is_meta and msg.type == "end_of_track":
            eot_tick = tick
    assert eot_tick >= last_note_tick == 720
    # Deltas are non-negative and the track still parses.
    assert all(m.time >= 0 for m in track)


def test_same_tick_insert_off_before_on():
    # Inserted note ends exactly where the next performed note starts.
    ref = make_midi(tracks=[notes_track([60, 62, 64], dur=240, gap=0)])
    perf = make_midi(tracks=[notes_track([60, 64], dur=240, gap=240)])
    r = revise(ref, perf, [{"op": "insert_missed", "ref_index": 1}])
    assert r.status_code == 200
    mid, _ = unzip(r)
    kinds = [(m.type, getattr(m, "note", None)) for m in mid.tracks[1]
             if not m.is_meta]
    # At tick 240 the new note_off(62) precedes the note_on(64).
    off_idx = kinds.index(("note_off", 62))
    on_idx = kinds.index(("note_on", 64))
    assert off_idx < on_idx
    # And the revised file is accepted by the strict loader.
    r2 = client.post("/compare", files={
        "reference": ("r.mid", ref, "audio/midi"),
        "performance": ("p.mid", zipfile.ZipFile(io.BytesIO(r.content))
                        .read("revised.mid"), "audio/midi"),
    }, data={"ref_track": 1, "ref_channel": 0,
             "perf_track": 1, "perf_channel": 0})
    assert r2.json()["errors"]["missed"]["count"] == 0


def test_reject_different_pitch_overlap():
    events = [
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_on", note=64, velocity=80, time=100),
        mido.Message("note_off", note=60, velocity=0, time=100),
        mido.Message("note_off", note=64, velocity=0, time=100),
    ]
    bad = make_midi(tracks=[events])
    r = client.post("/compare", files={
        "reference": ("r.mid", bad, "audio/midi"),
        "performance": ("p.mid", REF, "audio/midi"),
    }, data={"ref_track": 1, "ref_channel": 0,
             "perf_track": 1, "perf_channel": 0})
    assert r.status_code == 422
    assert "pitches 60 and 64" in r.json()["detail"]["message"]


def test_reject_zero_track_file():
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    buf = io.BytesIO()
    mid.save(file=buf)
    r = client.post("/compare", files={
        "reference": ("r.mid", buf.getvalue(), "audio/midi"),
        "performance": ("p.mid", REF, "audio/midi"),
    }, data={"ref_track": 0, "ref_channel": 0,
             "perf_track": 1, "perf_channel": 0})
    assert r.status_code == 422
    assert "no tracks" in r.json()["detail"]["message"]
