"""SMF parsing, tempo timeline and monophonic melody extraction."""

import io
from fractions import Fraction

import mido

from .errors import MidiRejection

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_TRACKS = 32
MAX_EVENTS = 40000
MAX_NOTES = 1024
DEFAULT_TEMPO_US = 500000  # microseconds per quarter note


class Note:
    __slots__ = (
        "index",
        "event_index",
        "end_event_index",
        "pitch",
        "start_tick",
        "end_tick",
        "start_us",
        "end_us",
        "start_ms",
        "end_ms",
    )

    def __init__(self, index, event_index, end_event_index, pitch,
                 start_tick, end_tick, start_us, end_us):
        self.index = index
        self.event_index = event_index
        self.end_event_index = end_event_index
        self.pitch = pitch
        self.start_tick = start_tick
        self.end_tick = end_tick
        self.start_us = start_us
        self.end_us = end_us
        self.start_ms = float(start_us) / 1000.0
        self.end_ms = float(end_us) / 1000.0

    @property
    def duration_ms(self):
        return round(self.end_ms - self.start_ms, 3)

    def to_dict(self):
        return {
            "index": self.index,
            "event_index": self.event_index,
            "end_event_index": self.end_event_index,
            "pitch": self.pitch,
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
            "start_ms": round(self.start_ms, 3),
            "end_ms": round(self.end_ms, 3),
            "duration_ms": self.duration_ms,
        }


def _reject(message, file, track=None, event=None):
    raise MidiRejection(message, file=file, track=track, event=event)


def load_midi(data, file_label):
    """Validate envelope limits and parse the SMF bytes."""
    if len(data) > MAX_FILE_BYTES:
        _reject("file exceeds 2 MiB limit", file_label)
    if not data:
        _reject("empty file", file_label)
    try:
        mid = mido.MidiFile(file=io.BytesIO(data))
    except Exception as exc:
        _reject(f"not a valid Standard MIDI File: {exc}", file_label)
    if mid.type == 2:
        _reject("SMF Type 2 is not supported", file_label)
    # SMPTE division has the high bit set; only positive PPQN is accepted.
    if mid.ticks_per_beat <= 0 or mid.ticks_per_beat & 0x8000:
        _reject("SMPTE time division is not supported; positive PPQN required",
                file_label)
    if len(mid.tracks) > MAX_TRACKS:
        _reject(f"file has {len(mid.tracks)} tracks, limit is {MAX_TRACKS}",
                file_label)
    if not mid.tracks:
        _reject("file has no tracks", file_label)
    total_events = sum(len(t) for t in mid.tracks)
    if total_events > MAX_EVENTS:
        _reject(f"file has {total_events} events, limit is {MAX_EVENTS}",
                file_label)
    return mid


class TempoMap:
    """Tick<->time converter built from the tempo map.

    Time is kept as exact rational microseconds; milliseconds are derived.
    """

    def __init__(self, segments, start_us, ppqn):
        self._segments = segments      # (start_tick, tempo_us)
        self._start_us = start_us      # Fraction microseconds at segment start
        self._ppqn = ppqn

    def _segment_index(self, tick):
        lo, hi = 0, len(self._segments) - 1
        while lo < hi:
            mid_i = (lo + hi + 1) // 2
            if self._segments[mid_i][0] <= tick:
                lo = mid_i
            else:
                hi = mid_i - 1
        return lo

    def tick_to_us(self, tick):
        i = self._segment_index(tick)
        start_tick, tempo = self._segments[i]
        return self._start_us[i] + Fraction((tick - start_tick) * tempo,
                                            self._ppqn)

    def tick_to_ms(self, tick):
        return float(self.tick_to_us(tick)) / 1000.0

    def us_to_tick_fraction(self, us):
        """Exact inverse: absolute microseconds -> fractional tick."""
        lo, hi = 0, len(self._segments) - 1
        while lo < hi:
            mid_i = (lo + hi + 1) // 2
            if self._start_us[mid_i] <= us:
                lo = mid_i
            else:
                hi = mid_i - 1
        start_tick, tempo = self._segments[lo]
        return Fraction(start_tick) + Fraction(
            (us - self._start_us[lo]) * self._ppqn, tempo)


def build_timeline(mid, file_label):
    """Return a TempoMap based on the tempo map of the file.

    Type 1 takes tempo events from track 0; Type 0 from its only track.
    Defaults to 500000 us per quarter. Zero tempo and two tempo events on
    the same tick are rejected. Time is accumulated rationally per segment.
    """
    tempo_track = mid.tracks[0]
    tempo_events = []  # (tick, event_index, tempo)
    tick = 0
    for idx, msg in enumerate(tempo_track):
        tick += msg.time
        if msg.type == "set_tempo":
            if msg.tempo <= 0:
                _reject("zero tempo is not allowed", file_label, 0, idx)
            tempo_events.append((tick, idx, msg.tempo))

    seen_ticks = set()
    for t, idx, _ in tempo_events:
        if t in seen_ticks:
            _reject("duplicate tempo event on the same tick", file_label, 0,
                    idx)
        seen_ticks.add(t)

    segments = []  # (start_tick, tempo_us)
    if not tempo_events or tempo_events[0][0] > 0:
        segments.append((0, DEFAULT_TEMPO_US))
    segments.extend((t, tempo) for t, _, tempo in tempo_events)
    segments.sort(key=lambda s: s[0])

    # Rational absolute time (microseconds) at each segment start.
    start_us = [Fraction(0)]
    for i in range(1, len(segments)):
        delta = segments[i][0] - segments[i - 1][0]
        start_us.append(start_us[-1] + Fraction(delta * segments[i - 1][1],
                                                mid.ticks_per_beat))
    return TempoMap(segments, start_us, mid.ticks_per_beat)


def extract_melody(mid, track_index, channel, tempo_map, file_label):
    """Extract a strict monophonic melody from one track and channel.

    Delta ticks are accumulated; a note_on with positive velocity on the
    selected channel opens a note, note_off or zero-velocity note_on closes
    it. At the same tick closes are processed before opens. Isolated closes,
    unclosed notes, zero-length notes and overlaps are rejected. Messages on
    other channels and pedals are ignored.
    """
    if track_index >= len(mid.tracks):
        _reject(f"track {track_index} does not exist "
                f"(file has {len(mid.tracks)} tracks)", file_label,
                track_index)
    track = mid.tracks[track_index]

    # Group relevant note events per tick, closes before opens.
    per_tick = []  # entries: [tick, closes=[(event_idx, pitch)], opens=[...]]
    tick = 0
    current = None
    for idx, msg in enumerate(track):
        if msg.time:
            if current is not None:
                per_tick.append(current)
                current = None
            tick += msg.time
        if msg.type not in ("note_on", "note_off"):
            continue
        if msg.channel != channel:
            continue
        if current is None:
            current = [tick, [], []]
        if msg.type == "note_off" or (msg.type == "note_on"
                                      and msg.velocity == 0):
            current[1].append((idx, msg.note))
        else:
            current[2].append((idx, msg.note))
    if current is not None:
        per_tick.append(current)

    open_notes = {}  # pitch -> (start_tick, event_index)
    melody = []  # (start_tick, end_tick, event_index, end_event_index, pitch)
    for t, closes, opens in per_tick:
        for idx, pitch in closes:
            if pitch not in open_notes:
                _reject(f"isolated note off for pitch {pitch}", file_label,
                        track_index, idx)
            start_tick, start_idx = open_notes.pop(pitch)
            if t == start_tick:
                _reject(f"zero length note for pitch {pitch}", file_label,
                        track_index, idx)
            melody.append((start_tick, t, start_idx, idx, pitch))
        for idx, pitch in opens:
            if pitch in open_notes:
                _reject(f"overlapping note for pitch {pitch}", file_label,
                        track_index, idx)
            if open_notes:
                other = next(iter(open_notes))
                _reject(f"overlapping notes for pitches {other} and {pitch}",
                        file_label, track_index, idx)
            open_notes[pitch] = (t, idx)
    if open_notes:
        pitch, (_, idx) = next(iter(open_notes.items()))
        _reject(f"unclosed note for pitch {pitch}", file_label, track_index,
                idx)

    melody.sort(key=lambda n: (n[0], n[2]))
    if not melody:
        _reject("melody is empty (need 1 to 1024 notes)", file_label,
                track_index)
    if len(melody) > MAX_NOTES:
        _reject(f"melody has {len(melody)} notes, limit is {MAX_NOTES}",
                file_label, track_index)

    notes = []
    for i, (start_tick, end_tick, event_idx, end_event_idx, pitch) in \
            enumerate(melody):
        notes.append(Note(i, event_idx, end_event_idx, pitch, start_tick,
                          end_tick, tempo_map.tick_to_us(start_tick),
                          tempo_map.tick_to_us(end_tick)))
    return notes
