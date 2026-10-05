"""Selective revision of a performance SMF driven by alignment errors.

Actions reference the original /compare alignment by error category and
note index; the alignment is computed once and never re-derived from the
action order. Three operations are supported:

- fix_pitch:    retune a wrong-pitch performed note to the reference
                pitch, keeping its ticks and velocity.
- delete_extra: remove the on/off events of an extra performed note.
- insert_missed: insert a missed reference note using the reference
                pitch and absolute start/end time, converted to
                performance ticks through the performance tempo map with
                exact rational math and half-up rounding; velocity 80;
                timing is absolute from tick 0 (no shifting).

All actions are planned together. After rounding, a zero-length note, a
negative time or any overlap on the selected channel rejects the whole
request and locates the offending action.
"""

import hashlib
import io
import json
import zipfile

import mido

from .errors import MidiRejection
from .midi_loader import MAX_EVENTS, MAX_FILE_BYTES

MAX_ACTIONS = 1024
INSERT_VELOCITY = 80

_OPS = ("fix_pitch", "delete_extra", "insert_missed")


def _reject(message, action=None, track=None, event=None):
    raise MidiRejection(message, file="performance", track=track,
                        event=event, action=action)


def parse_actions(raw):
    """Parse and structurally validate the JSON action list (1..1024)."""
    try:
        actions = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MidiRejection(f"actions is not valid JSON: {exc}",
                            file="performance")
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise MidiRejection(
            f"actions must be a list of 1 to {MAX_ACTIONS} items",
            file="performance")
    for i, act in enumerate(actions):
        if not isinstance(act, dict) or act.get("op") not in _OPS:
            _reject(f"action {i} must be an object with op in "
                    f"{'/'.join(_OPS)}", action=i)
        key = "ref_index" if act["op"] == "insert_missed" else "perf_index"
        idx = act.get(key)
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            _reject(f"action {i} needs a non-negative integer {key}",
                    action=i)
    return actions


def _round_half_up(frac):
    """Round a non-negative Fraction to the nearest int, halves go up."""
    return (2 * frac.numerator + frac.denominator) // (2 * frac.denominator)


def plan_revision(actions, alignment, ref_notes, perf_notes, perf_tempo):
    """Validate actions against the original alignment and plan edits.

    Rejects actions that reference nonexistent errors, duplicate targets
    or contradictory operations on the same performed note.
    """
    wrong = {it["perf_index"]: it
             for it in alignment["errors"]["wrong_pitch"]["items"]}
    extra = {it["perf_index"]
             for it in alignment["errors"]["extra"]["items"]}
    missed = {it["ref_index"]
              for it in alignment["errors"]["missed"]["items"]}

    used_perf = {}  # perf_index -> action index (fix/delete share targets)
    used_ref = {}   # ref_index -> action index
    ops = []
    for ai, act in enumerate(actions):
        op = act["op"]
        if op == "fix_pitch":
            pi = act["perf_index"]
            if pi not in wrong:
                _reject(f"no wrong-pitch error at performance note {pi}",
                        action=ai)
            if pi in used_perf:
                _reject(f"conflicting actions for performance note {pi}",
                        action=ai)
            used_perf[pi] = ai
            note = perf_notes[pi]
            ops.append({
                "op": op, "action": ai, "perf_index": pi,
                "pitch_before": note.pitch,
                "pitch_after": wrong[pi]["ref_pitch"],
                "start_tick": note.start_tick, "end_tick": note.end_tick,
            })
        elif op == "delete_extra":
            pi = act["perf_index"]
            if pi not in extra:
                _reject(f"no extra-note error at performance note {pi}",
                        action=ai)
            if pi in used_perf:
                _reject(f"conflicting actions for performance note {pi}",
                        action=ai)
            used_perf[pi] = ai
            note = perf_notes[pi]
            ops.append({
                "op": op, "action": ai, "perf_index": pi,
                "pitch_before": note.pitch, "pitch_after": None,
                "start_tick": note.start_tick, "end_tick": note.end_tick,
            })
        else:  # insert_missed
            ri = act["ref_index"]
            if ri not in missed:
                _reject(f"no missed-note error at reference note {ri}",
                        action=ai)
            if ri in used_ref:
                _reject(f"duplicate insert for reference note {ri}",
                        action=ai)
            used_ref[ri] = ai
            ref = ref_notes[ri]
            if ref.start_us < 0 or ref.end_us < 0:
                _reject(f"reference note {ri} has negative time", action=ai)
            start_tick = _round_half_up(
                perf_tempo.us_to_tick_fraction(ref.start_us))
            end_tick = _round_half_up(
                perf_tempo.us_to_tick_fraction(ref.end_us))
            if start_tick < 0:
                _reject(f"insert for reference note {ri} rounds to a "
                        f"negative tick", action=ai)
            if end_tick <= start_tick:
                _reject(f"insert for reference note {ri} rounds to a "
                        f"zero-length note", action=ai)
            ops.append({
                "op": op, "action": ai, "ref_index": ri,
                "pitch_before": None, "pitch_after": ref.pitch,
                "start_tick": start_tick, "end_tick": end_tick,
                "start_ms": round(ref.start_ms, 3),
                "end_ms": round(ref.end_ms, 3),
            })

    # Unified overlap check on the final selected-channel note set.
    deleted = {op["perf_index"] for op in ops if op["op"] == "delete_extra"}
    intervals = [(n.start_tick, n.end_tick, None)
                 for i, n in enumerate(perf_notes) if i not in deleted]
    intervals += [(op["start_tick"], op["end_tick"], op["action"])
                  for op in ops if op["op"] == "insert_missed"]
    intervals.sort(key=lambda iv: (iv[0], iv[1]))
    prev_end = 0
    for start, end, action in intervals:
        if start < prev_end:
            _reject("resulting notes overlap on the selected channel",
                    action=action)
        prev_end = max(prev_end, end)
    return ops


def apply_revision(mid, track_index, channel, ops, perf_notes):
    """Apply planned ops to the performance track and rebuild deltas.

    Unmodified events keep their content, absolute tick and relative
    order. New note_offs sort before note_ons on the same tick. The
    track's end_of_track is postponed when new events pass it.
    """
    track = mid.tracks[track_index]
    events = []  # [abs_tick, priority, msg]; priority orders same-tick inserts
    tick = 0
    for msg in track:
        tick += msg.time
        events.append([tick, 1, msg])

    note_by_index = {n.index: n for n in perf_notes}
    removed = set()
    for op in ops:
        if op["op"] == "insert_missed":
            on = mido.Message("note_on", note=op["pitch_after"],
                              velocity=INSERT_VELOCITY, time=0,
                              channel=channel)
            off = mido.Message("note_off", note=op["pitch_after"],
                               velocity=0, time=0, channel=channel)
            events.append([op["start_tick"], 2, on])
            events.append([op["end_tick"], 0, off])
            continue
        note = note_by_index[op["perf_index"]]
        if op["op"] == "fix_pitch":
            events[note.event_index][2].note = op["pitch_after"]
            events[note.end_event_index][2].note = op["pitch_after"]
        else:  # delete_extra
            removed.add(note.event_index)
            removed.add(note.end_event_index)

    kept = [e for i, e in enumerate(events) if i not in removed]
    for e in kept:
        if e[2].is_meta and e[2].type == "end_of_track":
            e[1] = 3
    others_max = max((e[0] for e in kept if e[1] != 3), default=0)
    for e in kept:
        if e[1] == 3:
            e[0] = max(e[0], others_max)  # postpone track end if needed
    kept.sort(key=lambda e: (e[0], e[1]))

    new_track = mido.MidiTrack()
    prev = 0
    for abs_tick, _, msg in kept:
        msg.time = abs_tick - prev
        prev = abs_tick
        new_track.append(msg)
    track[:] = new_track
    return mid


def build_revision_zip(ref_bytes, perf_bytes, mid, ops):
    """Serialize the revised SMF, enforce source limits, build ZIP+audit."""
    buf = io.BytesIO()
    mid.save(file=buf)
    revised = buf.getvalue()
    if len(revised) > MAX_FILE_BYTES:
        raise MidiRejection("revised file exceeds 2 MiB limit",
                            file="performance")
    total_events = sum(len(t) for t in mid.tracks)
    if total_events > MAX_EVENTS:
        raise MidiRejection(
            f"revised file has {total_events} events, limit is {MAX_EVENTS}",
            file="performance")

    ordered = sorted(ops, key=lambda o: o["action"])
    audit = {
        "reference_sha256": hashlib.sha256(ref_bytes).hexdigest(),
        "performance_sha256": hashlib.sha256(perf_bytes).hexdigest(),
        "revised_sha256": hashlib.sha256(revised).hexdigest(),
        "action_count": len(ordered),
        "actions": [dict(op) for op in ordered],
    }

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("revised.mid", revised)
        zf.writestr("audit.json",
                    json.dumps(audit, ensure_ascii=False, indent=2))
    return out.getvalue()
