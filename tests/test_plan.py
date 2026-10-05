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


def plan(midi_bytes, **form):
    data = {
        "track": form.get("track", 1),
        "channel": form.get("channel", 0),
    }
    if "open_strings" in form:
        data["open_strings"] = form["open_strings"]
    files = {"file": ("guitar.mid", midi_bytes, "audio/midi")}
    return client.post("/plan", files=files, data=data)


def unzip(resp):
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    plan_doc = json.loads(zf.read("plan.json"))
    mid = mido.MidiFile(file=io.BytesIO(zf.read("annotated.mid")))
    return plan_doc, mid


def example(name):
    with open(os.path.join(EXAMPLES, name), "rb") as fh:
        return fh.read()


def test_plan_example_zip():
    src = example("guitar.mid")
    r = plan(src)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    doc, mid = unzip(r)
    assert doc["source_sha256"] == hashlib.sha256(src).hexdigest()
    assert doc["track"] == 1
    assert doc["channel"] == 0
    assert doc["open_strings"] == [64, 59, 55, 50, 45, 40]
    assert doc["note_count"] == 26
    assert len(doc["notes"]) == 26
    for i, item in enumerate(doc["notes"]):
        assert item["index"] == i
        assert 1 <= item["string"] <= 6
        assert 0 <= item["fret"] <= 20
        assert 1 <= item["position"] <= 17
        if item["fret"] > 0:
            assert item["position"] <= item["fret"] <= item["position"] + 3
        assert item["pitch"] - doc["open_strings"][item["string"] - 1] \
            == item["fret"]
    assert doc["total_position_movement"] >= 0
    assert doc["total_string_changes"] >= 0
    # annotated MIDI keeps type/PPQN and carries one text per note
    assert mid.type == 1
    assert mid.ticks_per_beat == 480
    texts = [m for m in mid.tracks[1]
             if m.is_meta and m.type == "text"]
    assert len(texts) == 26
    assert "string" in texts[0].text and "position" in texts[0].text


def test_plan_annotations_precede_note_on_same_tick():
    src = make_midi(tracks=[notes_track([64, 59, 55])])
    r = plan(src)
    assert r.status_code == 200
    _, mid = unzip(r)
    track = mid.tracks[1]
    tick = 0
    last_text = None
    seen = 0
    for msg in track:
        tick += msg.time
        if msg.is_meta and msg.type == "text":
            last_text = (tick, msg.text)
        elif msg.type == "note_on" and msg.velocity > 0:
            assert last_text is not None and last_text[0] == tick
            assert f"plan note {seen}:" in last_text[1]
            seen += 1
            last_text = None
    assert seen == 3


def test_plan_open_string_shift_window():
    # 64 (open string 1) then 40 (open string 6): no shift needed, but
    # force positions via a fretted middle note far away.
    src = make_midi(tracks=[notes_track([64, 84, 64], dur=240, gap=240)])
    r = plan(src)
    assert r.status_code == 200
    doc, _ = unzip(r)
    notes = doc["notes"]
    assert notes[1]["pitch"] == 84
    # 84 = string 1 fret 20 -> position 17 forced
    assert notes[1]["string"] == 1
    assert notes[1]["fret"] == 20
    assert notes[1]["position"] == 17


def test_plan_unreachable_fast_shift():
    # 41 (string 6 fret 1, position 1 forced) then 84 (string 1 fret 20,
    # position 17 forced). The shift window is the gap between the first
    # note's end and the second onset: 240 ticks = 250 ms at the default
    # tempo, but the jump is 16 frets = 320 ms. Unreachable.
    src = make_midi(tracks=[notes_track([41, 84], dur=240, gap=0)])
    r = plan(src)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "unreachable" in detail["message"]
    assert "note 1" in detail["message"]
    assert detail["file"] == "guitar"


def test_plan_exact_time_equality_is_reachable():
    # 47 can be string 6 fret 7 (position up to 7); 84 forces position
    # 17. The fretted gap is 192 ticks = 200 ms at the default tempo,
    # exactly the 10-fret shift from position 7. Equality is reachable.
    events = [
        mido.Message("note_on", note=47, velocity=80, time=0, channel=0),
        mido.Message("note_off", note=47, velocity=0, time=100,
                     channel=0),
        mido.Message("note_on", note=84, velocity=80, time=192,
                     channel=0),
        mido.Message("note_off", note=84, velocity=0, time=240,
                     channel=0),
    ]
    src = make_midi(tracks=[events])
    r = plan(src)
    assert r.status_code == 200
    doc, _ = unzip(r)
    notes = doc["notes"]
    assert notes[0]["position"] == 7
    assert notes[1]["position"] == 17
    assert doc["total_position_movement"] == 10


def test_plan_no_candidate_pitch():
    src = make_midi(tracks=[notes_track([64, 20])])  # 20 below string 6
    r = plan(src)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "no playable string/fret candidate" in detail["message"]
    assert "note 1" in detail["message"]


def test_plan_custom_open_strings():
    src = make_midi(tracks=[notes_track([60, 62])])
    r = plan(src, open_strings="60,59,55,50,45,40")
    assert r.status_code == 200
    doc, _ = unzip(r)
    assert doc["open_strings"] == [60, 59, 55, 50, 45, 40]
    assert doc["notes"][0]["string"] == 1
    assert doc["notes"][0]["fret"] == 0


def test_plan_bad_open_strings():
    src = make_midi(tracks=[notes_track([64])])
    assert plan(src, open_strings="64,59,55").status_code == 422
    assert plan(src, open_strings="64,59,55,50,45,128").status_code == 422
    assert plan(src, open_strings="64,59,55,50,45,x").status_code == 422


def test_plan_global_not_greedy():
    # 64 then 67 then 64. A per-note greedy pick (lowest string/fret/
    # position triple) takes 64 as string 1 open; the global optimum
    # must still be found and be unique and deterministic.
    src = make_midi(tracks=[notes_track([64, 67, 64], dur=240, gap=240)])
    r1 = plan(src)
    r2 = plan(src)
    assert r1.status_code == r2.status_code == 200
    doc1, _ = unzip(r1)
    doc2, _ = unzip(r2)
    assert doc1["notes"] == doc2["notes"]
    seq = [(n["string"], n["fret"], n["position"]) for n in doc1["notes"]]
    # movement total matches the note sequence
    movement = sum(abs(seq[i + 1][2] - seq[i][2])
                   for i in range(len(seq) - 1))
    assert doc1["total_position_movement"] == movement
    changes = sum(1 for i in range(len(seq) - 1)
                  if seq[i + 1][0] != seq[i][0])
    assert doc1["total_string_changes"] == changes


def test_plan_missing_file():
    r = client.post("/plan", data={"track": 1, "channel": 0})
    assert r.status_code == 422
    assert r.json()["detail"]["file"] == "guitar"


def test_compare_long_text_smf_accepted():
    # A legal SMF carrying a >1 MiB text meta event must not be falsely
    # rejected (mido's 1 MB message cap aligned with the 2 MiB limit).
    events = [mido.MetaMessage("text", text="a" * 1_500_000, time=0)]
    events += notes_track([60, 62])
    src = make_midi(tracks=[events])
    assert len(src) > 1_000_000
    data = {"ref_track": 1, "ref_channel": 0,
            "perf_track": 1, "perf_channel": 0}
    files = {"reference": ("r.mid", src, "audio/midi"),
             "performance": ("p.mid", src, "audio/midi")}
    r = client.post("/compare", files=files, data=data)
    assert r.status_code == 200
    assert r.json()["total_cost"] == 0


def test_revise_overlap_attributes_insert_action():
    # Inserted missed note starts before an existing note and overlaps
    # it: the rejection must locate the insert action, not None.
    ref = make_midi(tracks=[notes_track([62, 60], dur=240, gap=240)])
    # performance keeps only the 60, at the same absolute time (tick 480)
    perf_events = [
        mido.Message("note_on", note=60, velocity=80, time=480,
                     channel=0),
        mido.Message("note_off", note=60, velocity=0, time=240,
                     channel=0),
    ]
    perf = make_midi(tracks=[perf_events])
    # reference 62 spans tick 0-240; make it longer so the insert
    # overlaps the existing 60 at tick 480.
    ref_events = [
        mido.Message("note_on", note=62, velocity=80, time=0, channel=0),
        mido.Message("note_off", note=62, velocity=0, time=600,
                     channel=0),
        mido.Message("note_on", note=60, velocity=80, time=0, channel=0),
        mido.Message("note_off", note=60, velocity=0, time=120,
                     channel=0),
    ]
    ref = make_midi(tracks=[ref_events])
    actions = [{"op": "insert_missed", "ref_index": 0}]
    data = {"ref_track": 1, "ref_channel": 0, "perf_track": 1,
            "perf_channel": 0, "actions": json.dumps(actions)}
    files = {"reference": ("r.mid", ref, "audio/midi"),
             "performance": ("p.mid", perf, "audio/midi")}
    r = client.post("/revise", files=files, data=data)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "overlap" in detail["message"]
    assert detail["action"] == 0
