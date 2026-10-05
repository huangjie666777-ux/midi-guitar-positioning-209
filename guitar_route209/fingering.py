"""Whole-piece guitar fingering planning for a monophonic melody.

Each note maps to candidate (string, fret, position) states. Strings 1-6
have open pitches 64/59/55/50/45/40 by default (overridable). The fret is
pitch minus open pitch, valid 0-20. Positions are 1-17; a pressed fret
must lie within position..position+3, an open string suits any position.

Shifting the position by one fret costs 20 ms. While the previous note
is pressed the shift must fit between its end and the next onset; when
the previous note is an open string the shift may start at its onset.
Time comparisons use the exact rational tempo timeline; equality is
reachable. The first note position is free.

A global DP minimizes, in order: total position movement, number of
string changes, and finally the lexicographic sequence of
(string, fret, position) triples, yielding one unique solution. Notes
are never planned greedily one by one. If a note has no candidate or no
reachable state, the first failing note is located and no partial plan
is delivered.
"""

import hashlib
import io
import json
import zipfile

import mido

from .errors import MidiRejection
from .midi_loader import MAX_EVENTS, MAX_FILE_BYTES

DEFAULT_OPEN_STRINGS = (64, 59, 55, 50, 45, 40)  # strings 1..6
MAX_FRET = 20
MAX_POSITION = 17
POSITION_SPAN = 3          # pressed fret within position..position+3
SHIFT_US_PER_FRET = 20000  # 20 ms per position fret


def _reject(message, note=None, track=None):
    raise MidiRejection(message, file="file", track=track, note=note)


def parse_open_strings(raw):
    """Validate the optional open-string override (six ints 0-127)."""
    if raw is None:
        return list(DEFAULT_OPEN_STRINGS)
    try:
        values = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MidiRejection(f"open_strings is not valid JSON: {exc}",
                            file="file")
    if (not isinstance(values, list) or len(values) != 6
            or any(not isinstance(v, int) or isinstance(v, bool)
                   or not 0 <= v <= 127 for v in values)):
        raise MidiRejection(
            "open_strings must be a JSON list of six integers 0-127",
            file="file")
    return values


def _candidates(pitch, open_strings):
    """All (string, fret, position) states for one pitch, sorted."""
    states = []
    for string, open_pitch in enumerate(open_strings, start=1):
        fret = pitch - open_pitch
        if not 0 <= fret <= MAX_FRET:
            continue
        if fret == 0:
            positions = range(1, MAX_POSITION + 1)
        else:
            lo = max(1, fret - POSITION_SPAN)
            hi = min(MAX_POSITION, fret)
            positions = range(lo, hi + 1)
        states.extend((string, fret, p) for p in positions)
    states.sort()
    return states


def plan_fingering(notes, open_strings):
    """Globally optimal fingering for the whole melody.

    Returns (assignments, total_movement, string_changes) where
    assignments[i] is (string, fret, position) for notes[i]. Rejects
    with the first note that has no candidate or no reachable state.
    """
    cand = [_candidates(n.pitch, open_strings) for n in notes]
    for i, states in enumerate(cand):
        if not states:
            _reject(f"note {i} (pitch {notes[i].pitch}) has no playable "
                    f"string/fret candidate", note=i)

    # dp state -> (movement, string_changes, triple sequence so far)
    prev_best = {s: (0, 0, (s,)) for s in cand[0]}
    parents = []  # per note (from index 1): {state: previous state}
    for i in range(1, len(notes)):
        prv, cur = notes[i - 1], notes[i]
        cur_best = {}
        parent = {}
        for s2, f2, p2 in cand[i]:
            best = None
            best_prev = None
            for s1, f1, p1 in cand[i - 1]:
                entry = prev_best.get((s1, f1, p1))
                if entry is None:
                    continue
                movement = abs(p2 - p1)
                # Pressed previous note: shift only in the gap after its
                # end; open string: shift may start at its onset.
                anchor = prv.end_us if f1 > 0 else prv.start_us
                if movement * SHIFT_US_PER_FRET > cur.start_us - anchor:
                    continue
                key = (entry[0] + movement,
                       entry[1] + (1 if s2 != s1 else 0))
                seq = entry[2] + ((s2, f2, p2),)
                if (best is None or key < best[:2]
                        or (key == best[:2] and seq < best[2])):
                    best = key + (seq,)
                    best_prev = (s1, f1, p1)
            if best is not None:
                cur_best[(s2, f2, p2)] = best
                parent[(s2, f2, p2)] = best_prev
        if not cur_best:
            _reject(f"note {i} (pitch {cur.pitch}) has no reachable "
                    f"position within the available shift time", note=i)
        parents.append(parent)
        prev_best = cur_best

    final_state, (movement, changes, _) = min(
        prev_best.items(), key=lambda kv: kv[1])
    assignments = [final_state]
    for parent in reversed(parents):
        assignments.append(parent[assignments[-1]])
    assignments.reverse()
    return assignments, movement, changes


def annotate_midi(mid, track_index, notes, assignments):
    """Insert a text meta event before each original note_on (same tick).

    The text records index, string, fret and position. Type, PPQN, all
    event fields, absolute ticks and the relative order of original
    events are preserved; deltas are rebuilt non-negative.
    """
    track = mid.tracks[track_index]
    events = []  # [abs_tick, order, msg]
    tick = 0
    for idx, msg in enumerate(track):
        tick += msg.time
        events.append([tick, 2 * idx, msg])

    by_index = {n.index: n for n in notes}
    for i, (string, fret, position) in enumerate(assignments):
        note = by_index[i]
        text = (f"plan: index={i} string={string} fret={fret} "
                f"position={position}")
        events.append([note.start_tick, 2 * note.event_index - 1,
                       mido.MetaMessage("text", text=text, time=0)])

    events.sort(key=lambda e: (e[0], e[1]))
    new_track = mido.MidiTrack()
    prev = 0
    for abs_tick, _, msg in events:
        msg.time = abs_tick - prev
        prev = abs_tick
        new_track.append(msg)
    track[:] = new_track
    return mid


def build_plan_zip(source_bytes, mid, track, channel, open_strings,
                   notes, assignments, movement, changes):
    """Serialize the annotated SMF, enforce source limits, build ZIP."""
    buf = io.BytesIO()
    mid.save(file=buf)
    annotated = buf.getvalue()
    if len(annotated) > MAX_FILE_BYTES:
        raise MidiRejection("annotated file exceeds 2 MiB limit",
                            file="file")
    total_events = sum(len(t) for t in mid.tracks)
    if total_events > MAX_EVENTS:
        raise MidiRejection(
            f"annotated file has {total_events} events, "
            f"limit is {MAX_EVENTS}", file="file")

    plan = {
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "annotated_sha256": hashlib.sha256(annotated).hexdigest(),
        "track": track,
        "channel": channel,
        "open_strings": list(open_strings),
        "note_count": len(notes),
        "total_position_movement": movement,
        "string_changes": changes,
        "notes": [
            {
                "index": n.index,
                "start_tick": n.start_tick,
                "pitch": n.pitch,
                "string": a[0],
                "fret": a[1],
                "position": a[2],
            }
            for n, a in zip(notes, assignments)
        ],
    }

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("annotated.mid", annotated)
        zf.writestr("plan.json",
                    json.dumps(plan, ensure_ascii=False, indent=2))
    return out.getvalue()
