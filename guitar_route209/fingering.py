"""Whole-piece guitar fingering planning over the monophonic melody.

Strings 1-6 default to open pitches 64/59/55/50/45/40 (overridable).
Fret = pitch - open pitch, valid 0-20. A position is an integer 1-17;
a fretted note must lie within position..position+3, an open string
fits any position. Shifting the position by one fret costs 20 ms:
while the previous note is fretted the shift must fit between its end
and the next onset; while it is an open string the shift may start at
its onset. Times use the exact rational tempo map; equality is
reachable. The first note's position is free.

The plan minimizes, lexicographically: total position movement, then
string changes, then the sequence of (string, fret, position) triples.
This is a global dynamic program (suffix costs plus an exact forward
reconstruction), never a per-note greedy choice. Failure locates the
first note with no candidate or no reachable state; no partial plan is
delivered.
"""

import hashlib
import io
import json
import zipfile
from fractions import Fraction

import mido

from .errors import MidiRejection
from .midi_loader import MAX_EVENTS, MAX_FILE_BYTES

DEFAULT_OPEN_STRINGS = (64, 59, 55, 50, 45, 40)
MAX_FRET = 20
MIN_POSITION = 1
MAX_POSITION = 17
POSITION_SPAN = 3
SHIFT_US_PER_FRET = 20 * 1000  # 20 ms per fret of position movement

FILE_LABEL = "guitar"


def parse_open_strings(raw):
    """Parse the optional open-string override: six integers 0-127."""
    if raw is None:
        return list(DEFAULT_OPEN_STRINGS)
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 6:
        raise MidiRejection(
            "open_strings must list six integers (strings 1 to 6)",
            file=FILE_LABEL)
    strings = []
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            raise MidiRejection(
                f"open string pitch {part!r} is not an integer",
                file=FILE_LABEL)
        if not 0 <= value <= 127:
            raise MidiRejection(
                f"open string pitch {value} is outside 0-127",
                file=FILE_LABEL)
        strings.append(value)
    return strings


def _candidates(pitch, open_strings):
    """Playable (string, fret) options for a pitch, string order 1-6."""
    out = []
    for string, open_pitch in enumerate(open_strings, start=1):
        fret = pitch - open_pitch
        if 0 <= fret <= MAX_FRET:
            out.append((string, fret))
    return out


def _positions(fret):
    if fret == 0:
        return range(MIN_POSITION, MAX_POSITION + 1)
    lo = max(MIN_POSITION, fret - POSITION_SPAN)
    hi = min(MAX_POSITION, fret)
    return range(lo, hi + 1)


def _states_for_note(note, open_strings):
    """All (string, fret, position) states of one note, sorted."""
    states = []
    for string, fret in _candidates(note.pitch, open_strings):
        for position in _positions(fret):
            states.append((string, fret, position))
    return states


def _reachable(prev_note, prev_state, note, position):
    """Exact-time reachability of a position shift between two notes."""
    _, prev_fret, prev_position = prev_state
    needed = abs(position - prev_position) * SHIFT_US_PER_FRET
    if needed == 0:
        return True
    if prev_fret == 0:
        available = note.start_us - prev_note.start_us
    else:
        available = note.start_us - prev_note.end_us
    return Fraction(needed) <= available


def _transition(prev_state, state):
    """(position movement, string changes) added by one transition."""
    return (abs(state[2] - prev_state[2]),
            1 if state[0] != prev_state[0] else 0)


def plan_fingering(notes, open_strings, track_index):
    """Globally optimal fingering for the whole melody.

    Returns (sequence of (string, fret, position), total movement,
    total string changes). Raises MidiRejection locating the first
    note with no candidate or no reachable state.
    """
    states_per_note = []
    for note in notes:
        states = _states_for_note(note, open_strings)
        if not states:
            raise MidiRejection(
                f"note {note.index} (pitch {note.pitch}) has no playable "
                f"string/fret candidate (fret must be 0-{MAX_FRET})",
                file=FILE_LABEL, track=track_index,
                event=note.event_index)
        states_per_note.append(states)

    n = len(notes)

    # Forward reachability (any cost) locates the first note that no
    # state can reach from the previous note.
    reachable = set(states_per_note[0])
    for i in range(1, n):
        cur = set()
        for state in states_per_note[i]:
            for prev_state in reachable:
                if _reachable(notes[i - 1], prev_state, notes[i],
                              state[2]):
                    cur.add(state)
                    break
        if not cur:
            note = notes[i]
            raise MidiRejection(
                f"note {note.index} (pitch {note.pitch}) is unreachable: "
                f"no position shift from note {notes[i - 1].index} fits "
                f"the available time",
                file=FILE_LABEL, track=track_index,
                event=note.event_index)
        reachable = cur

    # Suffix DP: suff[i][state] = minimal (movement, changes) of the
    # transitions from note i in that state to the end of the piece.
    suff = [None] * n
    suff[n - 1] = {state: (0, 0) for state in states_per_note[n - 1]}
    for i in range(n - 2, -1, -1):
        row = {}
        next_note = notes[i + 1]
        for state in states_per_note[i]:
            best = None
            for nxt, tail in suff[i + 1].items():
                if not _reachable(notes[i], state, next_note, nxt[2]):
                    continue
                step = _transition(state, nxt)
                cost = (step[0] + tail[0], step[1] + tail[1])
                if best is None or cost < best:
                    best = cost
            if best is not None:
                row[state] = best
        suff[i] = row

    # Forward pass: prefix cost plus suffix optimum must equal the
    # global optimum; among feasible states pick the smallest triple.
    if not suff[0]:
        note = notes[0]
        raise MidiRejection(
            f"note {note.index} (pitch {note.pitch}) is unreachable",
            file=FILE_LABEL, track=track_index, event=note.event_index)
    optimal = min(suff[0].values())

    sequence = []
    prev_state = None
    spent = (0, 0)
    for i, note in enumerate(notes):
        chosen = None
        for state in states_per_note[i]:
            tail = suff[i].get(state)
            if tail is None:
                continue
            if prev_state is None:
                step = (0, 0)
            else:
                if not _reachable(notes[i - 1], prev_state, note,
                                  state[2]):
                    continue
                step = _transition(prev_state, state)
            if (spent[0] + step[0] + tail[0],
                    spent[1] + step[1] + tail[1]) == optimal:
                chosen = state
                break  # states are sorted: first feasible is smallest
        if chosen is None:
            raise MidiRejection(
                f"note {note.index} (pitch {note.pitch}) is unreachable: "
                f"no position shift from note {note.index - 1} fits the "
                f"available time",
                file=FILE_LABEL, track=track_index,
                event=note.event_index)
        sequence.append(chosen)
        if prev_state is not None:
            step = _transition(prev_state, chosen)
            spent = (spent[0] + step[0], spent[1] + step[1])
        prev_state = chosen

    return sequence, spent[0], spent[1]


def annotate_midi(mid, track_index, notes, sequence):
    """Insert a text meta event before each original note_on, same tick.

    The text marks index, string, fret and position. Type, PPQN, all
    tracks, event content, absolute ticks and relative order are
    preserved; end_of_track is postponed if annotations pass it.
    """
    track = mid.tracks[track_index]
    events = []  # [abs_tick, priority, msg]
    tick = 0
    for msg in track:
        tick += msg.time
        events.append([tick, 1, msg])

    for note, (string, fret, position) in zip(notes, sequence):
        text = mido.MetaMessage(
            "text",
            text=f"plan note {note.index}: string {string} "
                 f"fret {fret} position {position}",
            time=0)
        events.append([note.start_tick, 0, text])

    for e in events:
        if e[2].is_meta and e[2].type == "end_of_track":
            e[1] = 3
    others_max = max((e[0] for e in events if e[1] != 3), default=0)
    for e in events:
        if e[1] == 3:
            e[0] = max(e[0], others_max)
    events.sort(key=lambda e: (e[0], e[1]))

    new_track = mido.MidiTrack()
    prev = 0
    for abs_tick, _, msg in events:
        msg.time = abs_tick - prev
        prev = abs_tick
        new_track.append(msg)
    track[:] = new_track
    return mid


def build_plan_zip(source_bytes, mid, notes, sequence, movement, changes,
                   open_strings, track_index, channel):
    """Serialize the annotated SMF, enforce output limits, build ZIP."""
    buf = io.BytesIO()
    mid.save(file=buf)
    annotated = buf.getvalue()
    if len(annotated) > MAX_FILE_BYTES:
        raise MidiRejection("annotated file exceeds 2 MiB limit",
                            file=FILE_LABEL)
    total_events = sum(len(t) for t in mid.tracks)
    if total_events > MAX_EVENTS:
        raise MidiRejection(
            f"annotated file has {total_events} events, "
            f"limit is {MAX_EVENTS}", file=FILE_LABEL)

    plan = {
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "track": track_index,
        "channel": channel,
        "open_strings": list(open_strings),
        "note_count": len(notes),
        "total_position_movement": movement,
        "total_string_changes": changes,
        "notes": [
            {
                "index": note.index,
                "event_index": note.event_index,
                "pitch": note.pitch,
                "start_tick": note.start_tick,
                "end_tick": note.end_tick,
                "string": state[0],
                "fret": state[1],
                "position": state[2],
            }
            for note, state in zip(notes, sequence)
        ],
    }

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plan.json",
                    json.dumps(plan, ensure_ascii=False, indent=2))
        zf.writestr("annotated.mid", annotated)
    return out.getvalue()
