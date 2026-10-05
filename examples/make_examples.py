"""Generate real example MIDI files used by the README and tests.

Run: .venv/bin/python examples/make_examples.py
"""

import os

import mido

PPQN = 480
HERE = os.path.dirname(os.path.abspath(__file__))


def note(track, kind, pitch, vel, time, channel=0):
    track.append(mido.Message(kind, note=pitch, velocity=vel, time=time,
                              channel=channel))


def melody_track(name, pitches, dur=240, gap=240, channel=0):
    track = mido.MidiTrack()
    track.name = name
    first = True
    for pitch in pitches:
        note(track, "note_on", pitch, 80, 0 if first else gap, channel)
        note(track, "note_off", pitch, 0, dur, channel)
        first = False
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def make_reference():
    mid = mido.MidiFile(type=1, ticks_per_beat=PPQN)
    tempo = mido.MidiTrack()
    tempo.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    tempo.append(mido.MetaMessage("set_tempo", tempo=400000, time=PPQN * 4))
    tempo.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tempo)
    mid.tracks.append(melody_track("ref", [60, 62, 64, 65, 67]))
    mid.save(os.path.join(HERE, "reference.mid"))


def make_performance():
    # Same melody but: plays 63 instead of 64 (wrong pitch), inserts an
    # extra 70, and starts 50ms late with shorter notes.
    mid = mido.MidiFile(type=1, ticks_per_beat=PPQN)
    tempo = mido.MidiTrack()
    tempo.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    tempo.append(mido.MetaMessage("set_tempo", tempo=400000, time=PPQN * 4))
    tempo.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tempo)
    track = mido.MidiTrack()
    track.name = "perf"
    # 50 ms late at 500000 us/qn, 480 PPQN: 0.05s -> 48 ticks.
    note(track, "note_on", 60, 90, 48)
    note(track, "note_off", 60, 0, 200)
    for pitch in (62, 63, 65, 67):  # 63 replaces 64
        note(track, "note_on", pitch, 90, 280)
        note(track, "note_off", pitch, 0, 200)
    note(track, "note_on", 70, 90, 280)  # extra note
    note(track, "note_off", 70, 0, 200)
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(track)
    mid.save(os.path.join(HERE, "performance.mid"))


def make_bad_overlap():
    mid = mido.MidiFile(type=0, ticks_per_beat=PPQN)
    track = mido.MidiTrack()
    note(track, "note_on", 60, 80, 0)
    note(track, "note_on", 60, 80, 100)  # overlap: 60 already open
    note(track, "note_off", 60, 0, 100)
    note(track, "note_off", 60, 0, 100)
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(track)
    mid.save(os.path.join(HERE, "bad_overlap.mid"))


def make_type2():
    mid = mido.MidiFile(type=2, ticks_per_beat=PPQN)
    mid.tracks.append(melody_track("a", [60]))
    mid.tracks.append(melody_track("b", [62]))
    mid.save(os.path.join(HERE, "bad_type2.mid"))


if __name__ == "__main__":
    make_reference()
    make_performance()
    make_bad_overlap()
    make_type2()
    print("wrote examples/*.mid")
