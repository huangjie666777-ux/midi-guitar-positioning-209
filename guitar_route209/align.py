"""Global order-preserving alignment of two monophonic melodies.

Dynamic programming over the two note sequences. Pairing equal pitches
costs 0, pairing different pitches costs 2, a missed reference note
(deletion) and an extra performed note (insertion) each cost 1.
Backtracking prefers, among equal-cost predecessors: pair, then missed,
then extra. Every note of both melodies appears in exactly one step.
"""


def align(ref_notes, perf_notes):
    n, m = len(ref_notes), len(perf_notes)
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sub = 0 if ref_notes[i - 1].pitch == perf_notes[j - 1].pitch else 2
            dp[i][j] = min(
                dp[i - 1][j - 1] + sub,  # pair
                dp[i - 1][j] + 1,        # missed reference note
                dp[i][j - 1] + 1,        # extra performed note
            )

    pairs = []   # (ref_index, perf_index)
    missed = []  # ref_index
    extra = []   # perf_index
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            sub = (0 if ref_notes[i - 1].pitch == perf_notes[j - 1].pitch
                   else 2)
            if dp[i][j] == dp[i - 1][j - 1] + sub:
                pairs.append((i - 1, j - 1))
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            missed.append(i - 1)
            i -= 1
            continue
        # Remaining case: j > 0 and dp[i][j] == dp[i][j - 1] + 1
        extra.append(j - 1)
        j -= 1

    pairs.reverse()
    missed.reverse()
    extra.reverse()

    pair_items = []
    correct = []
    wrong_pitch = []
    for ri, pi in pairs:
        ref, perf = ref_notes[ri], perf_notes[pi]
        item = {
            "ref_index": ri,
            "perf_index": pi,
            "ref_pitch": ref.pitch,
            "perf_pitch": perf.pitch,
            "onset_ms_deviation": round(perf.start_ms - ref.start_ms, 3),
            "duration_ms_deviation": round(perf.duration_ms
                                           - ref.duration_ms, 3),
        }
        pair_items.append(item)
        if ref.pitch == perf.pitch:
            correct.append(item)
        else:
            wrong_pitch.append(item)

    missed_items = [
        {
            "ref_index": ri,
            "ref_pitch": ref_notes[ri].pitch,
            "start_tick": ref_notes[ri].start_tick,
            "start_ms": round(ref_notes[ri].start_ms, 3),
        }
        for ri in missed
    ]
    extra_items = [
        {
            "perf_index": pi,
            "perf_pitch": perf_notes[pi].pitch,
            "start_tick": perf_notes[pi].start_tick,
            "start_ms": round(perf_notes[pi].start_ms, 3),
        }
        for pi in extra
    ]

    return {
        "total_cost": dp[n][m],
        "correct_count": len(correct),
        "pairs": pair_items,
        "errors": {
            "wrong_pitch": {"count": len(wrong_pitch), "items": wrong_pitch},
            "missed": {"count": len(missed_items), "items": missed_items},
            "extra": {"count": len(extra_items), "items": extra_items},
        },
    }
