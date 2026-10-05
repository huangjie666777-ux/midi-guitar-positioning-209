import io
import os
import sys

import mido
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guitar_route209.main import app  # noqa: E402

client = TestClient(app)
EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "examples")


def post(ref_bytes, perf_bytes, **form):
    data = {
        "ref_track": form.get("ref_track", 1),
        "ref_channel": form.get("ref_channel", 0),
        "perf_track": form.get("perf_track", 1),
        "perf_channel": form.get("perf_channel", 0),
    }
    files = {
        "reference": ("ref.mid", ref_bytes, "audio/midi"),
        "performance": ("perf.mid", perf_bytes, "audio/midi"),
    }
    return client.post("/compare", files=files, data=data)


def example(name):
    with open(os.path.join(EXAMPLES, name), "rb") as fh:
        return fh.read()


def make_midi(mtype=1, ppqn=480, tracks=None, tempos=None):
    mid = mido.MidiFile(type=mtype, ticks_per_beat=ppqn)
    if mtype == 1:
        t0 = mido.MidiTrack()
        if tempos is None:
            tempos = [(0, 500000)]
        for tick_time, tempo in tempos:
            t0.append(mido.MetaMessage("set_tempo", tempo=tempo,
                                       time=tick_time))
        t0.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(t0)
    for events in (tracks or []):
        tr = mido.MidiTrack()
        tr.extend(events)
        tr.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(tr)
    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()


def notes_track(pitches, dur=240, gap=240, channel=0):
    events = []
    first = True
    for p in pitches:
        events.append(mido.Message("note_on", note=p, velocity=80,
                                   time=0 if first else gap, channel=channel))
        events.append(mido.Message("note_off", note=p, velocity=0, time=dur,
                                   channel=channel))
        first = False
    return events


REF = make_midi(tracks=[notes_track([60, 62, 64])])


def test_identical_performance():
    r = post(REF, REF)
    assert r.status_code == 200
    body = r.json()
    assert body["total_cost"] == 0
    assert body["correct_count"] == 3
    assert body["errors"]["wrong_pitch"]["count"] == 0
    assert body["errors"]["missed"]["count"] == 0
    assert body["errors"]["extra"]["count"] == 0
    for pair in body["pairs"]:
        assert pair["onset_ms_deviation"] == 0
        assert pair["duration_ms_deviation"] == 0


def test_example_files():
    r = post(example("reference.mid"), example("performance.mid"))
    assert r.status_code == 200
    body = r.json()
    # wrong pitch (64->63) costs 2, extra 70 costs 1.
    assert body["total_cost"] == 3
    assert body["correct_count"] == 4
    assert body["errors"]["wrong_pitch"]["count"] == 1
    assert body["errors"]["missed"]["count"] == 0
    assert body["errors"]["extra"]["count"] == 1
    wp = body["errors"]["wrong_pitch"]["items"][0]
    assert (wp["ref_pitch"], wp["perf_pitch"]) == (64, 63)
    assert body["errors"]["extra"]["items"][0]["perf_pitch"] == 70
    # Performance starts 50 ms late; deviations are not re-based.
    assert body["pairs"][0]["onset_ms_deviation"] == pytest.approx(50.0)
    # 200 ticks vs 240 ticks at 500000 us/qn -> 208.333 ms vs 250 ms.
    assert body["pairs"][0]["duration_ms_deviation"] == pytest.approx(
        -41.667, abs=1e-3)


def test_missed_note():
    perf = make_midi(tracks=[notes_track([60, 64])])
    r = post(REF, perf)
    body = r.json()
    assert body["total_cost"] == 1
    assert body["errors"]["missed"]["count"] == 1
    assert body["errors"]["missed"]["items"][0]["ref_pitch"] == 62

def test_timing_uses_tempo_segments():
    # Performance at 250000 us/qn is twice as fast: second note onset at
    # 480 ticks = 250 ms vs 500 ms in the default-tempo reference.
    perf = make_midi(tracks=[notes_track([60, 62])], tempos=[(0, 250000)])
    r = post(make_midi(tracks=[notes_track([60, 62])]), perf)
    body = r.json()
    assert body["pairs"][1]["onset_ms_deviation"] == pytest.approx(-250.0)
    assert body["pairs"][1]["duration_ms_deviation"] == pytest.approx(-125.0)


def test_type0_single_track():
    ref = make_midi(mtype=0, tracks=[notes_track([60, 61])])
    r = post(ref, ref, ref_track=0, perf_track=0)
    assert r.status_code == 200
    assert r.json()["total_cost"] == 0


def test_reject_type2():
    r = post(example("bad_type2.mid"), REF)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["file"] == "reference"
    assert "Type 2" in d["message"]


def test_reject_smpte():
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    mid.ticks_per_beat = -6360  # SMPTE: 0xE728 as signed short
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr)
    buf = io.BytesIO()
    mid.save(file=buf)
    r = post(buf.getvalue(), REF, ref_track=0)
    assert r.status_code == 422
    assert "SMPTE" in r.json()["detail"]["message"]


def test_reject_overlap_locates_event():
    r = post(example("bad_overlap.mid"), REF, ref_track=0)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["file"] == "reference"
    assert d["track"] == 0
    assert d["event"] == 1
    assert "overlap" in d["message"]


def test_reject_isolated_off():
    bad = make_midi(tracks=[[mido.Message("note_off", note=60, velocity=0,
                                          time=10)]])
    r = post(bad, REF)
    assert r.status_code == 422
    assert "isolated" in r.json()["detail"]["message"]


def test_reject_unclosed():
    bad = make_midi(tracks=[[mido.Message("note_on", note=60, velocity=80,
                                          time=0)]])
    r = post(bad, REF)
    assert r.status_code == 422
    assert "unclosed" in r.json()["detail"]["message"]


def test_reject_zero_length():
    bad = make_midi(tracks=[[
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_off", note=60, velocity=0, time=0),
    ]])
    r = post(bad, REF)
    assert r.status_code == 422
    # Same-tick closes run before opens, so this surfaces as an isolated
    # note off (the zero-length guard covers remaining orderings).
    assert r.json()["detail"]["message"] in (
        "isolated note off for pitch 60", "zero length note for pitch 60")


def test_same_tick_close_before_open():
    events = [
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_off", note=60, velocity=0, time=240),
        mido.Message("note_on", note=60, velocity=80, time=0),
        mido.Message("note_off", note=60, velocity=0, time=240),
    ]
    r = post(make_midi(tracks=[events]), REF)
    assert r.status_code == 200


def test_reject_duplicate_tempo_same_tick():
    t0 = mido.MidiTrack()
    t0.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    t0.append(mido.MetaMessage("set_tempo", tempo=400000, time=0))
    t0.append(mido.MetaMessage("end_of_track", time=0))
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    mid.tracks.append(t0)
    tr = mido.MidiTrack()
    tr.extend(notes_track([60]))
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr)
    buf = io.BytesIO()
    mid.save(file=buf)
    r = post(buf.getvalue(), REF)
    assert r.status_code == 422
    assert "duplicate tempo" in r.json()["detail"]["message"]


def test_reject_zero_tempo():
    bad = make_midi(tracks=[notes_track([60])], tempos=[(0, 0)])
    r = post(bad, REF)
    assert r.status_code == 422
    assert "zero tempo" in r.json()["detail"]["message"]


def test_reject_bad_channel():
    r = post(REF, REF, ref_channel=16)
    assert r.status_code == 422


def test_reject_missing_track():
    r = post(REF, REF, ref_track=5)
    assert r.status_code == 422
    assert "track 5" in r.json()["detail"]["message"]


def test_reject_oversize():
    r = post(b"x" * (2 * 1024 * 1024 + 1), REF)
    assert r.status_code == 422
    assert "2 MiB" in r.json()["detail"]["message"]


def test_reject_empty_melody():
    empty = make_midi(tracks=[notes_track([60], channel=1)])
    r = post(empty, REF, ref_channel=0)
    assert r.status_code == 422
    assert "empty" in r.json()["detail"]["message"]


def test_other_channel_ignored():
    events = notes_track([60], channel=0) + notes_track([64], channel=1)
    ref = make_midi(tracks=[events])
    r = post(ref, REF, ref_channel=0)
    assert r.status_code == 200
    assert r.json()["reference"]["note_count"] == 1


def test_requests_are_independent():
    assert post(REF, REF).status_code == 200
    assert post(example("bad_type2.mid"), REF).status_code == 422
    assert post(REF, REF).status_code == 200
