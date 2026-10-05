"""FastAPI surface for comparison and selective revision of MIDI."""

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from .align import align
from .errors import MidiRejection
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
async def compare(
    reference: UploadFile = File(...),
    performance: UploadFile = File(...),
    ref_track: int = Form(...),
    ref_channel: int = Form(...),
    perf_track: int = Form(...),
    perf_channel: int = Form(...),
):
    _check_selector(ref_track, ref_channel, "reference")
    _check_selector(perf_track, perf_channel, "performance")

    _, _, ref_notes, perf_notes, _, result = _analyze(
        await reference.read(), await performance.read(),
        ref_track, ref_channel, perf_track, perf_channel)
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


def _form_int(form, key, file_label):
    raw = form.get(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise MidiRejection(f"form field {key} must be an integer",
                            file_label)


@app.post("/revise")
async def revise(request: Request):
    # Parse the form ourselves with a 2 MiB part limit so a legal long
    # JSON action list (matching the file limit) is not falsely rejected
    # by the 1 MiB default for non-file form fields.
    form = await request.form(max_part_size=MAX_FILE_BYTES)
    reference = form.get("reference")
    performance = form.get("performance")
    if not isinstance(reference, StarletteUploadFile):
        raise MidiRejection("missing reference file", "reference")
    if not isinstance(performance, StarletteUploadFile):
        raise MidiRejection("missing performance file", "performance")
    raw_actions = form.get("actions")
    if isinstance(raw_actions, StarletteUploadFile):
        raw_actions = (await raw_actions.read()).decode("utf-8", "replace")
    if not isinstance(raw_actions, str):
        raise MidiRejection("missing actions JSON field", "performance")

    ref_track = _form_int(form, "ref_track", "reference")
    ref_channel = _form_int(form, "ref_channel", "reference")
    perf_track = _form_int(form, "perf_track", "performance")
    perf_channel = _form_int(form, "perf_channel", "performance")
    _check_selector(ref_track, ref_channel, "reference")
    _check_selector(perf_track, perf_channel, "performance")

    ref_bytes = await reference.read()
    perf_bytes = await performance.read()
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


@app.get("/health")
async def health():
    return {"status": "ok"}
