"""Beat-index computation for bass_retune tab coloring.

Pure-arithmetic copy of the quantization and slot/cursor logic from
tabview's rs2gp.py.  No guitarpro import; no external dependencies
beyond stdlib.  Produces {beat_index: flag_str} dicts whose keys match
the zero-based sequential beat indices that alphaTab assigns as g.bN
CSS classes across all measures in document order.

Maintenance note: if tabview's rs2gp.py changes its SUBDIV constant,
_quantize_sixteenth, _decompose_sixteenths, _dur_sixteenths, or the
slot/cursor loop in _create_beats_flags, update this file to match.
"""

SUBDIV = 8  # 32nd notes per beat (must match tabview rs2gp.py)


# ---------------------------------------------------------------------------
# Quantization helpers (mirrors rs2gp._quantize_sixteenth etc.)
# ---------------------------------------------------------------------------

def _quantize_sixteenth(event_time, beat_times, measure_end):
    """Return the nearest 32nd-note slot index within a measure."""
    best, best_d = 0, float("inf")
    for i, bt in enumerate(beat_times):
        nxt = beat_times[i + 1] if i + 1 < len(beat_times) else measure_end
        dur = nxt - bt
        for sub in range(SUBDIV):
            t = bt + dur * sub / SUBDIV
            d = abs(event_time - t)
            if d < best_d:
                best_d = d
                best = i * SUBDIV + sub
    return best


def _decompose_sixteenths(count):
    """Break a 32nd-note count into a list of duration values (as ints).

    Each returned int is the number of 32nd notes that duration occupies
    (mirrors the value sequence in rs2gp._decompose_sixteenths without
    creating guitarpro.Duration objects).
    """
    if count <= 0:
        return [8]  # quarter note fallback
    durs = []
    rem = count
    while rem > 0:
        if rem >= 32:
            durs.append(32); rem -= 32
        elif rem >= 24:
            durs.append(24); rem -= 24
        elif rem >= 16:
            durs.append(16); rem -= 16
        elif rem >= 12:
            durs.append(12); rem -= 12
        elif rem >= 8:
            durs.append(8);  rem -= 8
        elif rem >= 6:
            durs.append(6);  rem -= 6
        elif rem >= 4:
            durs.append(4);  rem -= 4
        elif rem >= 3:
            durs.append(3);  rem -= 3
        elif rem >= 2:
            durs.append(2);  rem -= 2
        else:
            durs.append(1);  rem -= 1
    return durs


# ---------------------------------------------------------------------------
# Slot/cursor loop (mirrors rs2gp._create_beats, flags-only)
# ---------------------------------------------------------------------------

def _create_beats_flags(events, m_info):
    """Return {local_beat_index: flag_str} for one measure.

    events: list of event dicts (same structure as rs2gp._merge_events output).
    m_info: measure info dict from rs2gp._parse_measures.

    The local beat index is the zero-based position of the beat within the
    sequence this measure produces — the caller offsets it to a global index.
    """
    total = m_info["num_beats"] * SUBDIV

    if not events:
        # All-rest measure: _rest_beats produces ceil(total / largest_fit) beats.
        # We don't need to count them precisely here — an all-rest measure has
        # no flags, so return empty.
        return {}, len(_decompose_sixteenths(total))

    slots = {}
    for ev in events:
        pos = _quantize_sixteenth(ev["time"], m_info["beat_times"], m_info["end_time"])
        pos = max(0, min(pos, total - 1))
        slots.setdefault(pos, []).append(ev)

    positions = sorted(slots.keys())
    flag_collector = {}
    beat_count = 0
    cursor = 0

    for i, pos in enumerate(positions):
        # Rest beats before this slot
        if pos > cursor:
            rest_durs = _decompose_sixteenths(pos - cursor)
            beat_count += len(rest_durs)
            cursor = pos

        nxt = positions[i + 1] if i + 1 < len(positions) else total
        gap = max(1, nxt - pos)
        durations = _decompose_sixteenths(gap)

        # This is the note beat — its local index is beat_count.
        local_idx = beat_count
        for ev in slots[pos]:
            note_dicts = (
                ev.get("chord_notes", []) if ev.get("type") == "chord" else [ev]
            )
            for nd in note_dicts:
                flag = nd.get("bass_retune_flag", "")
                if flag:
                    existing = flag_collector.get(local_idx)
                    if existing is None or flag == "octaveUp":
                        flag_collector[local_idx] = flag

        beat_count += 1  # the note beat itself
        cursor += durations[0]

        # Tied/split rest beats after this slot's first duration
        for rd in durations[1:]:
            beat_count += 1
            cursor += rd

    # Trailing rest beats
    if cursor < total:
        rest_durs = _decompose_sixteenths(total - cursor)
        beat_count += len(rest_durs)

    return flag_collector, beat_count


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_flags_map(events, measures_info, num_strings):
    """Compute {global_beat_index: flag_str} across all measures.

    Parameters match the data shapes used in rs2gp.rocksmith_to_gp5:
      events        — output of rs2gp._merge_events(arr)
      measures_info — output of rs2gp._parse_measures(song.beats)
      num_strings   — 4 for bass, 6 for guitar (used to filter invalid strings)

    The returned dict's keys are the same zero-based global beat indices that
    alphaTab uses for its g.bN CSS classes, so screen.js can query them directly.
    """
    flags_map: dict[int, str] = {}
    global_beat_offset = 0

    for m_info in measures_info:
        m_events = [
            e for e in events
            if m_info["start_time"] <= e["time"] < m_info["end_time"]
        ]

        # Filter out events on invalid strings (mirrors rs2gp._create_beats gp_str guard).
        filtered = []
        for ev in m_events:
            if ev.get("type") == "chord":
                valid_notes = [
                    nd for nd in ev.get("chord_notes", [])
                    if 1 <= (num_strings - nd.get("string", -1)) <= num_strings
                ]
                if valid_notes:
                    filtered.append(dict(ev, chord_notes=valid_notes))
            else:
                gp_str = num_strings - ev.get("string", -1)
                if 1 <= gp_str <= num_strings:
                    filtered.append(ev)

        local_flags, beat_count = _create_beats_flags(filtered, m_info)

        for local_idx, flag in local_flags.items():
            global_idx = global_beat_offset + local_idx
            existing = flags_map.get(global_idx)
            if existing is None or flag == "octaveUp":
                flags_map[global_idx] = flag

        global_beat_offset += beat_count

    return flags_map
