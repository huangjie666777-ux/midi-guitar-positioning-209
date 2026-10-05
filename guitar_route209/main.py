"""FastAPI surface: comparison, selective revision, fingering plan."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from .align import align
from .errors import MidiRejection
from .fingering import (annotate_midi, build_plan_zip, parse_open_strings,
                        plan_fingering)
from .midi_loader import (MAX_FILE_BYTES, build_timeline, extract_melody,
                          load_midi)
from .revise import (apply_revision, build_revision_zip, parse_actions,
                     plan_revision)

app = FastAPI(title="Monophonic MIDI Performance Comparator")


@app.exception_handler(MidiRejection)
async def rejection_handler(request, exc: MidiRejection):
    return JSONResponse(status_code=422, content={"detail": exc.to_detail()})


def _check_selector(track, channel, file_label):
    if track < 0:
        raise MidiRejection("track index must be >= 0", file_label)
    if not 0 <= channel <= 15:
        raise MidiRejection("channel must be between 0 and 15", file_label)


async def _part_bytes(form, key, file_label):
    """Read an SMF form part sent either as a file or as a text field."""
    part = form.get(key)
    if isinstance(part, StarletteUploadFile):
        return await part.read()
    if isinstance(part, str):
        return part.encode("utf-8")
    raise MidiRejection(f"missing {key} file", file_label)


def _form_int(form, key, file_label):
    raw = form.get(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise MidiRejection(f"form field {key} must be an integer",
                            file_label)


def _analyze(ref_bytes, perf_bytes, ref_track, ref_channel, perf_track,
             perf_channel):
    """Shared pipeline: parse, extract melodies and align both files."""
    ref_mid = load_midi(ref_bytes, "reference")
    perf_mid = load_midi(perf_bytes, "performance")
    ref_notes = extract_melody(ref_mid, ref_track, ref_channel,
                               build_timeline(ref_mid, "reference"),
                               "reference")
    perf_tempo = build_timeline(perf_mid, "performance")
    perf_notes = extract_melody(perf_mid, perf_track, perf_channel,
                                perf_tempo, "performance")
    return ref_mid, perf_mid, ref_notes, perf_notes, perf_tempo, \
        align(ref_notes, perf_notes)


@app.post("/compare")
async def compare(request: Request):
    # Parse the form ourselves with a 2 MiB part limit so a legal SMF
    # (matching the file limit) is not falsely rejected by the 1 MiB
    # default for non-file form fields.
    form = await request.form(max_part_size=MAX_FILE_BYTES)
    ref_bytes = await _part_bytes(form, "reference", "reference")
    perf_bytes = await _part_bytes(form, "performance", "performance")
    ref_track = _form_int(form, "ref_track", "reference")
    ref_channel = _form_int(form, "ref_channel", "reference")
    perf_track = _form_int(form, "perf_track", "performance")
    perf_channel = _form_int(form, "perf_channel", "performance")
    _check_selector(ref_track, ref_channel, "reference")
    _check_selector(perf_track, perf_channel, "performance")

    _, _, ref_notes, perf_notes, _, result = _analyze(
        ref_bytes, perf_bytes, ref_track, ref_channel, perf_track,
        perf_channel)
    result["reference"] = {
        "track": ref_track,
        "channel": ref_channel,
        "note_count": len(ref_notes),
        "notes": [n.to_dict() for n in ref_notes],
    }
    result["performance"] = {
        "track": perf_track,
        "channel": perf_channel,
        "note_count": len(perf_notes),
        "notes": [n.to_dict() for n in perf_notes],
    }
    return result


@app.post("/revise")
async def revise(request: Request):
    form = await request.form(max_part_size=MAX_FILE_BYTES)
    raw_actions = form.get("actions")
    if isinstance(raw_actions, StarletteUploadFile):
        raw_actions = (await raw_actions.read()).decode("utf-8", "replace")
    if not isinstance(raw_actions, str):
        raise MidiRejection("missing actions JSON field", "performance")

    ref_bytes = await _part_bytes(form, "reference", "reference")
    perf_bytes = await _part_bytes(form, "performance", "performance")
    ref_track = _form_int(form, "ref_track", "reference")
    ref_channel = _form_int(form, "ref_channel", "reference")
    perf_track = _form_int(form, "perf_track", "performance")
    perf_channel = _form_int(form, "perf_channel", "performance")
    _check_selector(ref_track, ref_channel, "reference")
    _check_selector(perf_track, perf_channel, "performance")

    actions = parse_actions(raw_actions)

    _, perf_mid, ref_notes, perf_notes, perf_tempo, alignment = _analyze(
        ref_bytes, perf_bytes, ref_track, ref_channel, perf_track,
        perf_channel)

    ops = plan_revision(actions, alignment, ref_notes, perf_notes,
                        perf_tempo)
    apply_revision(perf_mid, perf_track, perf_channel, ops, perf_notes)
    payload = build_revision_zip(ref_bytes, perf_bytes, perf_mid, ops)
    return Response(
        content=payload, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="revision.zip"'})


@app.post("/plan")
async def plan(request: Request):
    # Same 2 MiB part limit as the other endpoints.
    form = await request.form(max_part_size=MAX_FILE_BYTES)
    source = await _part_bytes(form, "file", "guitar")
    track = _form_int(form, "track", "guitar")
    channel = _form_int(form, "channel", "guitar")
    _check_selector(track, channel, "guitar")
    raw_strings = form.get("open_strings")
    if isinstance(raw_strings, StarletteUploadFile):
        raw_strings = (await raw_strings.read()).decode("utf-8", "replace")
    if raw_strings is not None and not isinstance(raw_strings, str):
        raise MidiRejection("open_strings must be six integers",
                            "guitar")
    open_strings = parse_open_strings(raw_strings)

    mid = load_midi(source, "guitar")
    tempo_map = build_timeline(mid, "guitar")
    notes = extract_melody(mid, track, channel, tempo_map, "guitar")

    sequence, movement, changes = plan_fingering(notes, open_strings,
                                                 track)
    annotate_midi(mid, track, notes, sequence)
    payload = build_plan_zip(source, mid, notes, sequence, movement,
                             changes, open_strings, track, channel)
    return Response(
        content=payload, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="plan.zip"'})


@app.get("/health")
async def health():
    return {"status": "ok"}
