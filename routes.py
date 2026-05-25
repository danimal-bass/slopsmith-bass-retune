"""Bass Tab Retune plugin — transpose bass arrangement frets to E standard tuning.

Reads the tuning header from the PSARC manifest JSON to determine per-string
semitone offsets, then rewrites all <note> and <chordNote> fret attributes in
the arrangement XML so the chart is playable on a standard-tuned (E A D G) bass.

Audio is never touched — only the tab data changes.

Negative-fret fallback: notes that would produce a negative fret after shifting
are first remapped to the next lower string at the same pitch (fret += STRING_INTERVAL).
Only when no lower string is available (already on string 0) or the lower-string fret
is still negative is the note moved up one octave to the next higher string.

SNG-only CDLCs (no source XML in the PSARC) are handled automatically: the bass
SNG is decompiled to XML via rscli, shifted, then recompiled back to SNG before
repacking. The XML is not included in the output PSARC.
"""

import bisect
import json
import logging
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger("slopsmith.plugins.bass_retune")

# Semitone distance between adjacent bass strings (all fourths = 5 semitones).
STRING_INTERVAL = 5

# Standard bass open-string names for display (low to high).
STRING_NAMES = ["E (low)", "A", "D", "G (high)"]

# rscli binary — same resolution strategy as retune.py
RSCLI = Path(os.environ.get("RSCLI_PATH", str(Path(__file__).parent / "tools" / "rscli" / "RsCli")))


# ── rscli helpers ─────────────────────────────────────────────────────────────

def _sng_to_xml(sng_path: Path, xml_path: Path) -> bool:
    """Decompile a Rocksmith SNG to XML using rscli. Returns True on success."""
    if not RSCLI.exists():
        return False
    r = subprocess.run(
        [str(RSCLI), "sng2xml", str(sng_path), str(xml_path)],
        capture_output=True,
    )
    return r.returncode == 0 and xml_path.exists() and xml_path.stat().st_size > 0


def _xml_to_sng(xml_path: Path, sng_path: Path) -> bool:
    """Recompile an arrangement XML back to SNG using rscli. Returns True on success."""
    if not RSCLI.exists():
        return False
    r = subprocess.run(
        [str(RSCLI), "xml2sng", str(xml_path), str(sng_path)],
        capture_output=True,
    )
    return r.returncode == 0 and sng_path.exists() and sng_path.stat().st_size > 0


# ── Manifest parsing ──────────────────────────────────────────────────────────

def _get_bass_tuning_offsets(psarc_path: str) -> dict:
    """Return {arrangement_id: {"name": str, "offsets": [int, int, int, int], "song_key": str}}
    for all Bass arrangements found in the PSARC manifest JSON.

    Offsets are the raw string0..string3 values from the Tuning dict —
    each is the semitone delta from E standard for that string.
    A fully E-standard bass arrangement has offsets [0, 0, 0, 0].

    song_key is included so callers can locate the arrangement SNG/XML by the
    standard CDLC naming convention: {song_key}_bass.sng / {song_key}_bass.xml.
    """

    from psarc import read_psarc_entries
    files = read_psarc_entries(psarc_path, ["*.json"])
    results = {}

    for path, data in sorted(files.items()):
        if not path.endswith(".json"):
            continue
        try:
            j = json.loads(data)
        except json.JSONDecodeError:
            import re
            text = data.decode("utf-8", errors="ignore")
            text = re.sub(r",\s*([}\]])", r"\1", text)
            try:
                j = json.loads(text)
            except Exception:
                continue

        for arr_id, v in j.get("Entries", {}).items():
            attrs = v.get("Attributes", {})
            arr_name = attrs.get("ArrangementName", "")
            if arr_name != "Bass":
                continue
            tun = attrs.get("Tuning")
            if not tun or not isinstance(tun, dict):
                continue
            offsets = [tun.get(f"string{i}", 0) for i in range(4)]
            song_key = attrs.get("SongKey", "")
            results[arr_id] = {"name": arr_name, "offsets": offsets, "song_key": song_key}

    return results


# ── XML / SNG location ────────────────────────────────────────────────────────

def _find_arrangement_xml(unpacked_dir: Path, arr_name: str, song_key: str) -> Path | None:
    """Locate the arrangement XML file using SongKey + arrangement name."""
    if song_key:
        target = f"{song_key.lower()}_{arr_name.lower()}.xml"
        for xml_file in unpacked_dir.rglob("*.xml"):
            if xml_file.name.lower() == target:
                return xml_file

    for xml_file in unpacked_dir.rglob("*.xml"):
        stem = xml_file.stem.lower()
        if (arr_name.lower() in stem
                and "showlights" not in stem
                and "vocals" not in stem):
            return xml_file

    return None


def _find_arrangement_sng(unpacked_dir: Path, arr_name: str, song_key: str) -> Path | None:
    """Locate the arrangement SNG file using SongKey + arrangement name."""
    if song_key:
        target = f"{song_key.lower()}_{arr_name.lower()}.sng"
        for sng_file in unpacked_dir.rglob("*.sng"):
            if sng_file.name.lower() == target:
                return sng_file

    for sng_file in unpacked_dir.rglob("*.sng"):
        stem = sng_file.stem.lower()
        if (arr_name.lower() in stem
                and "showlights" not in stem
                and "vocals" not in stem):
            return sng_file

    return None


def _get_xml_for_arrangement(
    unpacked_dir: Path,
    arr_name: str,
    song_key: str,
    log,
) -> tuple[Path | None, bool]:
    """Return (xml_path, decompiled_from_sng).

    Tries a pre-existing XML first; falls back to decompiling the SNG via
    rscli. Returns (None, False) if neither strategy succeeds.
    """
    xml_path = _find_arrangement_xml(unpacked_dir, arr_name, song_key)
    if xml_path:
        return xml_path, False

    sng_path = _find_arrangement_sng(unpacked_dir, arr_name, song_key)
    if not sng_path:
        log.warning(
            "bass_retune: no XML or SNG found for %s arrangement (song_key=%r)",
            arr_name, song_key,
        )
        return None, False

    if not RSCLI.exists():
        log.warning(
            "bass_retune: SNG found (%s) but rscli not available at %s",
            sng_path.name, RSCLI,
        )
        return None, False

    xml_out = sng_path.with_suffix(".xml")
    log.info("bass_retune: decompiling %s → %s", sng_path.name, xml_out.name)
    if not _sng_to_xml(sng_path, xml_out):
        log.warning("bass_retune: sng2xml failed for %s", sng_path.name)
        return None, False

    return xml_out, True


# ── Section lookup ────────────────────────────────────────────────────────────

def _build_section_map(root) -> list[tuple[float, str]]:
    """Return a sorted list of (start_time, section_name) from an arrangement XML root."""
    sections = []
    for sec in root.iter("section"):
        try:
            t = float(sec.get("startTime", sec.get("time", "-1")))
            name = sec.get("name", "").strip()
            if t >= 0 and name:
                sections.append((t, name))
        except (TypeError, ValueError):
            continue
    return sorted(sections)


def _section_for_time(sections: list[tuple[float, str]], time: float) -> str:
    """Return the section name that covers the given time, or '' if none."""
    name = ""
    for start, sec_name in sections:
        if start <= time:
            name = sec_name
        else:
            break
    return name


# ── Fret shifting ─────────────────────────────────────────────────────────────

def _shift_frets_in_xml(xml_path: Path, string_deltas: list[int]) -> dict:
    """Rewrite fret values in an arrangement XML file.

    string_deltas[i] = semitones to add to every note on string i.
    For Drop D, string0 offset = -2, so delta = -2: every note on string 0
    gets fret += -2 (fret moves down 2) so it sounds at the same pitch on a
    standard-tuned instrument. The tab was written with frets 2 higher than
    E standard to compensate for the lower open string, so we subtract 2 back.

    Returns {
        "shifted_per_string": {str_idx: count},
        "octave_up": int,
        "octave_up_sections": [str, ...],   # deduplicated section names
        "unchanged": int,
        "errors": [str],
    }
    """
    ET.register_namespace("", "")
    tree = ET.parse(xml_path)
    root = tree.getroot()

    sections = _build_section_map(root)

    stats = {
        "shifted_per_string": {},
        "octave_up": 0,
        "octave_up_sections": [],
        "unchanged": 0,
        "errors": [],
    }
    _octave_section_set: set[str] = set()

    def shift_note(elem):
        try:
            s = int(elem.get("string", -1))
            f = int(elem.get("fret", -1))
        except (TypeError, ValueError):
            return

        if s < 0 or s >= len(string_deltas):
            return
        if f < 0:
            return

        delta = string_deltas[s]
        if delta == 0:
            stats["unchanged"] += 1
            return

        new_fret = f + delta
        if new_fret >= 0:
            elem.set("fret", str(new_fret))
            stats["shifted_per_string"][s] = stats["shifted_per_string"].get(s, 0) + 1
        else:
            # new_fret is negative — the transposed position doesn't exist on this
            # string.  Resolution priority:
            #
            # 1. Try the next LOWER string (s-1).  Moving down one string raises
            #    the fret by STRING_INTERVAL (5 semitones, one perfect fourth) at
            #    the same pitch.  This keeps the note in the same octave and is
            #    always preferred over an octave jump when it produces a valid fret.
            #    Example: G#1-string fret 0, delta -1 → fret -1 on A-string;
            #    remapped to E-string fret 4 (-1 + 5 = 4). ✓
            #
            # 2. Only if no lower string is available (already on string 0) OR the
            #    lower-string fret is still negative, fall back to the next HIGHER
            #    string (s+1) at an octave up (fret + 12 - STRING_INTERVAL).  This
            #    is a genuine register change and is reported as "octave up".
            #
            # 3. If neither strategy yields a valid fret, log an error.

            # --- attempt 1: lower string, same octave ---
            if s > 0:
                fret_on_lower = new_fret + STRING_INTERVAL
                if fret_on_lower >= 0:
                    elem.set("string", str(s - 1))
                    elem.set("fret", str(fret_on_lower))
                    elem.set("bass_retune_flag", "stringDown")
                    stats["shifted_per_string"][s] = stats["shifted_per_string"].get(s, 0) + 1
                    return

            # --- attempt 2: higher string, octave up ---
            new_string = s + 1
            new_fret_on_string = new_fret + 12 - STRING_INTERVAL
            if new_string <= 3 and new_fret_on_string >= 0:
                elem.set("string", str(new_string))
                elem.set("fret", str(new_fret_on_string))
                elem.set("bass_retune_flag", "octaveUp")
                stats["octave_up"] += 1
                try:
                    t = float(elem.get("time", "-1"))
                    sec = _section_for_time(sections, t) if t >= 0 else ""
                    if sec and sec not in _octave_section_set:
                        _octave_section_set.add(sec)
                        stats["octave_up_sections"].append(sec)
                except (TypeError, ValueError):
                    pass
            else:
                stats["errors"].append(
                    f"Could not remap string={s} fret={f} "
                    f"(would be string={new_string} fret={new_fret_on_string})"
                )

    for note in root.iter("note"):
        shift_note(note)

    for chord_note in root.iter("chordNote"):
        shift_note(chord_note)

    # Update chord templates (fret0..fret3 attributes)
    # IMPORTANT: chordTemplate fretN indices run high-to-low (fret0=G high,
    # fret1=D, fret2=A, fret3=E low) — the reverse of string_deltas order
    # (index 0=E low, 1=A, 2=D, 3=G high). Reverse the index when looking
    # up the delta so the correct per-string shift is applied.
    n_strings = len(string_deltas)
    for tmpl in root.iter("chordTemplate"):
        for si in range(4):
            fret_attr = f"fret{si}"
            val = tmpl.get(fret_attr)
            if val is None:
                continue
            try:
                f = int(val)
            except ValueError:
                continue
            if f < 0:
                continue
            reversed_idx = (n_strings - 1) - si
            delta = string_deltas[reversed_idx] if 0 <= reversed_idx < n_strings else 0
            if delta == 0:
                continue
            new_f = f + delta
            if new_f >= 0:
                tmpl.set(fret_attr, str(new_f))
            else:
                # Can't remap an open/low-fret string within a chord template —
                # the fixed fretN slots don't support string reassignment.
                # Drop the string from the chord (sentinel -1 = not played)
                # rather than clamping to 0 which would play the wrong pitch.
                tmpl.set(fret_attr, "-1")
                tmpl.set(f"finger{si}", "-1")
                stats["errors"].append(
                    f"Chord template fret{si}={f} went negative; "
                    f"string dropped from chord (cannot remap within template)"
                )

    # Reset the tuning header to E standard
    tuning_elem = root.find(".//tuning")
    if tuning_elem is not None:
        for si in range(4):
            tuning_elem.set(f"string{si}", "0")

    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    return stats


# ── Pre-flight check ──────────────────────────────────────────────────────────

def _scan_for_negatives(psarc_path: str, filename: str, log) -> dict:
    """Pre-scan: unpack and check which notes would go negative without committing."""

    arr_tunings = _get_bass_tuning_offsets(psarc_path)
    if not arr_tunings:
        return {"eligible": False, "reason": "No bass arrangements found"}

    all_standard = all(d["offsets"] == [0, 0, 0, 0] for d in arr_tunings.values())
    if all_standard:
        return {"eligible": False, "reason": "Bass arrangement is already E standard"}

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        from psarc import unpack_psarc
        unpack_psarc(psarc_path, tmp)
        tmp_path = Path(tmp)

        for arr_id, arr_info in arr_tunings.items():
            offsets = arr_info["offsets"]
            song_key = arr_info["song_key"]
            deltas = list(offsets)

            xml_path, decompiled = _get_xml_for_arrangement(tmp_path, "Bass", song_key, log)
            if not xml_path:
                results.append({
                    "arr_id": arr_id,
                    "song_key": song_key,
                    "offsets": offsets,
                    "deltas": deltas,
                    "octave_up_count": 0,
                    "octave_up_notes": [],
                    "sng_only": True,
                    "rscli_available": RSCLI.exists(),
                })
                continue

            tree = ET.parse(xml_path)
            root = tree.getroot()
            sections = _build_section_map(root)

            octave_up_notes = []
            for note in root.iter("note"):
                try:
                    s = int(note.get("string", -1))
                    f = int(note.get("fret", -1))
                except (TypeError, ValueError):
                    continue
                if s < 0 or s >= len(deltas) or f < 0:
                    continue
                new_fret = f + deltas[s]
                if new_fret >= 0:
                    continue
                # Negative fret: check whether a lower-string remap resolves it
                # (same logic as shift_note attempt 1). Only flag as octave-up
                # when the lower-string escape is unavailable.
                if s > 0 and (new_fret + STRING_INTERVAL) >= 0:
                    continue  # will be remapped to lower string, no octave jump
                try:
                    t = float(note.get("time", "-1"))
                    sec = _section_for_time(sections, t) if t >= 0 else ""
                except (TypeError, ValueError):
                    t, sec = -1, ""
                octave_up_notes.append({
                    "time": note.get("time", "?"),
                    "string": s,
                    "fret": f,
                    "new_fret_would_be": new_fret,
                    "section": sec,
                })

            results.append({
                "arr_id": arr_id,
                "song_key": song_key,
                "offsets": offsets,
                "deltas": deltas,
                "octave_up_count": len(octave_up_notes),
                "octave_up_notes": octave_up_notes[:10],
                "sng_only": decompiled,
                "rscli_available": RSCLI.exists(),
            })

    return {
        "eligible": True,
        "filename": filename,
        "arrangements": results,
    }


# ── Non-bass arrangement removal ─────────────────────────────────────────────

def _remove_non_bass_arrangements(tmp_path: Path, log):
    """Prune all arrangement entries from hsan aggregate manifests.

    song.py reads both individual manifest JSONs ({song_key}_{arr}.json) and
    the hsan aggregate manifest to build the arrangement list.  If an
    arrangement appears in both, song.py counts it twice — producing duplicates
    in the player (2x Bass, 2x Lead, etc.).

    The individual JSONs are the authoritative source for arrangement discovery
    and must stay in the PSARC.  The hsan is only needed for the library
    scanner's tuning display, and that display is driven by whichever entry
    song.py reads first — so clearing all arrangement entries from the hsan
    (leaving only the structural shell) means the scanner falls back to the
    individual JSONs, which already reflect the corrected E-standard tuning
    for the bass arrangement.

    Infrastructure arrangements (Vocals, ShowLights, JVocals) are left in the
    hsan untouched because song.py does not duplicate-count them via individual
    JSONs.
    """
    # Names to strip from hsan — everything except infrastructure.
    KEEP_IN_HSAN = {"Vocals", "ShowLights", "JVocals", "Bass E-Std", ""}

    for hsan_path in tmp_path.rglob("*.hsan"):
        try:
            data = json.loads(hsan_path.read_bytes().decode("utf-8", errors="replace"))
            entries = data.get("Entries", {})
            to_delete = [
                k for k, v in entries.items()
                if v.get("Attributes", {}).get("ArrangementName", "") not in KEEP_IN_HSAN
            ]
            if to_delete:
                for k in to_delete:
                    del entries[k]
                hsan_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.info(
                    "bass_retune: cleared %d arrangement(s) from hsan %s",
                    len(to_delete), hsan_path.name,
                )
        except Exception as exc:
            log.warning("bass_retune: failed to prune hsan %s: %s", hsan_path.name, exc)


def _decompile_sng_only_arrangements(tmp_path: Path, log, skip_sng_stems: set[str] | None = None):
    """Decompile any SNG that has no paired XML in the unpacked directory.

    song.py uses a has_arrangement_xml guard: if *any* non-infrastructure XML
    exists in the unpacked directory it skips rscli decompilation entirely and
    builds the arrangement list purely from XMLs.  After conversion, estd_xml
    is intentionally kept in the PSARC (so Bass E-Std is discoverable), which
    means has_arrangement_xml is always True for converted songs.

    Without this function, Lead/Rhythm arrangements that were SNG-only in the
    original PSARC would have no XML and therefore no entry in song.py's
    arrangement list — they vanish from the player after conversion.

    This function uses rscli to produce a paired XML for every such SNG so
    song.py's XML-only path sees the complete set of arrangements.

    skip_sng_stems: lowercase SNG stems (without extension) that the convert
    loop deliberately left without a paired XML.  Typically this is the
    original bass SNG whose XML was decompiled as a working copy then deleted
    (decompiled_from_sng=True) — re-decompiling it here would pack an
    unshifted copy of the bass data and create a phantom duplicate arrangement.

    Infrastructure SNGs (showlights, vocals) are always skipped.

    If rscli is not available the function logs a warning and returns without
    error — the SNGs will still be in the PSARC and song.py will fall back to
    whatever behaviour it has when XMLs are missing (typically omitting the
    arrangement, which is the same outcome as before this fix).
    """
    SKIP_STEMS = ("showlights", "vocals", "jvocals")
    skip_sng_stems = {s.lower() for s in (skip_sng_stems or set())}

    if not RSCLI.exists():
        log.warning(
            "bass_retune: rscli not available at %s — SNG-only Lead/Rhythm arrangements "
            "will not have paired XMLs; they may be missing from the player after conversion",
            RSCLI,
        )
        return

    # song.py's XML discovery path (and _convert_sng_to_xml) uses songs/arr/
    # as the canonical XML directory.  Writing XMLs alongside the SNG in
    # songs/bin/generic/ causes load_song()'s rglob("*.xml") to pick them up
    # as extra arrangements — each one becomes a phantom duplicate in the
    # player because they have no <arrangement> element and fall back to the
    # filename-heuristic name (e.g. "Bass") instead of the manifest name.
    # Always write decompiled XMLs into the arr/ sibling of the bin/ dir so
    # they land in the same place _convert_sng_to_xml would put them.
    arr_dir: Path | None = None
    for candidate in tmp_path.rglob("songs/arr"):
        if candidate.is_dir():
            arr_dir = candidate
            break
    if arr_dir is None:
        # Derive from the first songs/bin directory we find.
        for candidate in tmp_path.rglob("songs/bin"):
            if candidate.is_dir():
                arr_dir = candidate.parent / "arr"
                arr_dir.mkdir(parents=True, exist_ok=True)
                break
    if arr_dir is None:
        # Last resort: create songs/arr/ directly under the extract root.
        arr_dir = tmp_path / "songs" / "arr"
        arr_dir.mkdir(parents=True, exist_ok=True)

    for sng_path in sorted(tmp_path.rglob("*.sng")):
        stem = sng_path.stem.lower()
        if any(skip in stem for skip in SKIP_STEMS):
            continue

        if stem in skip_sng_stems:
            log.debug(
                "bass_retune: skipping re-decompilation of %s (intentionally XML-free)",
                sng_path.name,
            )
            continue

        # Check for a paired XML in arr_dir (the canonical location), not
        # alongside the SNG in bin/generic/ — that location is never correct
        # and would create phantom duplicate arrangements in the player.
        xml_path = arr_dir / (sng_path.stem + ".xml")
        if xml_path.exists():
            # Already has a paired XML (either original source or one we wrote
            # earlier in the convert loop) — nothing to do.
            continue

        log.info(
            "bass_retune: decompiling SNG-only arrangement %s → %s",
            sng_path.name, xml_path.name,
        )
        if not _sng_to_xml(sng_path, xml_path):
            log.warning(
                "bass_retune: sng2xml failed for %s — arrangement may be missing "
                "from player after conversion",
                sng_path.name,
            )


# ── Add E-standard Bass arrangement ──────────────────────────────────────────

def _load_json_lenient(raw_bytes: bytes) -> dict:
    """Parse JSON bytes, stripping trailing commas on failure (common in CDLC tools)."""
    import re as _re
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        text = _re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(text)


def _add_estd_bass_arrangement(
    tmp_path: Path,
    arr_tunings: dict,
    log,
) -> dict:
    """Wire a second Bass arrangement (E standard) into manifests, hsan, and xblock.

    By the time this is called, the convert loop has already created
    ``{song_key}_bassestd.sng`` via rscli or fallback copy.  This function's
    job is purely metadata: clone the manifest JSON entry, hsan entry, and
    xblock entity so Rocksmith knows the new SNG exists and can present it as
    a selectable arrangement.

    Returns a dict of {song_key: new_arr_id} for logging.  An empty return
    means no arrangements were wired (treat as a soft failure).
    """
    import copy
    import uuid as _uuid

    added = {}

    for arr_id, arr_info in arr_tunings.items():
        song_key = arr_info["song_key"]
        new_urn_stem = f"{song_key.lower()}_bassestd" if song_key else None

        # Confirm the E-standard SNG was actually created before wiring metadata.
        # Search by exact name first (song_key known), then by any _bassestd.sng.
        estd_sng = None
        if song_key:
            for p in tmp_path.rglob("*.sng"):
                if p.name.lower() == f"{song_key.lower()}_bassestd.sng":
                    estd_sng = p
                    break
        if estd_sng is None:
            for p in tmp_path.rglob("*_bassestd.sng"):
                estd_sng = p
                # Derive urn stem from whatever file we found
                new_urn_stem = estd_sng.stem.lower()
                if not song_key:
                    # Best-effort: strip the _bassestd suffix to get the song_key
                    song_key = new_urn_stem[: -len("_bassestd")]
                break

        if estd_sng is None:
            log.warning(
                "bass_retune: _bassestd.sng not found for arr_id=%s song_key=%r — skipping wire-up",
                arr_id, arr_info["song_key"],
            )
            continue

        # Derive the original bass URN stem from the estd one
        orig_urn_stem = new_urn_stem[: -len("estd")]  # e.g. "songkey_bass"

        new_arr_id = str(_uuid.uuid4()).upper()
        new_persistent_id = new_arr_id.replace("-", "")

        # ── Clone manifest JSON entry ─────────────────────────────────────────
        # IMPORTANT: the new BassEstd entry must go in its OWN JSON file
        # named {song_key}_bassestd.json — NOT appended to the existing
        # bass JSON.  load_song() in song.py builds its arrangement-name
        # lookup keyed by JSON *filename stem* (not by entry GUID), so
        # both the original bass.json and the new bassestd.json entry for
        # an XML stem are resolved independently:
        #
        #   songkey_bass.xml     -> songkey_bass.json     -> "Bass"
        #   songkey_bassestd.xml -> songkey_bassestd.json -> "Bass E-Std"
        #
        # If both entries live in the same JSON file the stem key is
        # written twice (second write wins), one arrangement loses its
        # name, and the two arrangements show up swapped in the player.
        cloned_json = False
        source_entry = None
        source_json_path = None

        for json_path in sorted(tmp_path.rglob("*.json")):
            try:
                data = _load_json_lenient(json_path.read_bytes())
            except Exception as exc:
                log.warning("bass_retune: could not parse %s: %s", json_path.name, exc)
                continue

            entries = data.get("Entries", {})

            # Find the original bass entry — match by arr_id first, then by
            # scanning for ArrangementName=Bass as a fallback for CDLCs that
            # use lowercase or abbreviated GUIDs as keys.
            source_key = None
            if arr_id in entries:
                source_key = arr_id
            else:
                arr_id_norm = arr_id.upper().replace("-", "")
                for k, v in entries.items():
                    if (k.upper().replace("-", "") == arr_id_norm
                            or v.get("Attributes", {}).get("ArrangementName") == "Bass"):
                        source_key = k
                        break

            if source_key is None:
                continue

            source_entry = copy.deepcopy(entries[source_key])
            source_json_path = json_path
            break  # found — don't need to look further

        if source_entry is not None:
            new_entry = source_entry
            attrs = new_entry.get("Attributes", {})
            attrs["Tuning"] = {f"string{i}": 0 for i in range(6)}
            attrs["ArrangementId"] = new_arr_id
            attrs["PersistentID"] = new_persistent_id
            attrs["ArrangementName"] = "Bass E-Std"
            for key in ("SongXml", "SongBin"):
                if key in attrs:
                    attrs[key] = _replace_stem_ci(attrs[key], orig_urn_stem, new_urn_stem)

            # Write to a dedicated file so load_song's stem lookup is unambiguous.
            # Place it alongside the original bass JSON.
            estd_json_path = source_json_path.parent / f"{new_urn_stem}.json"
            estd_doc = {"Entries": {new_arr_id: new_entry}}
            try:
                estd_json_path.write_text(
                    json.dumps(estd_doc, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.debug("bass_retune: wrote E-std manifest to %s", estd_json_path.name)
                cloned_json = True
            except Exception as exc:
                log.warning("bass_retune: failed to write %s: %s", estd_json_path.name, exc)
        else:
            log.warning(
                "bass_retune: could not find arr_id=%s in any manifest JSON — "
                "E-standard arrangement will not be visible in-game",
                arr_id,
            )
            # Don't skip — still try hsan and xblock in case JSON was already patched.

        # ── Clone hsan aggregate manifest entry ───────────────────────────────
        for hsan_path in sorted(tmp_path.rglob("*.hsan")):
            try:
                data = _load_json_lenient(hsan_path.read_bytes())
            except Exception as exc:
                log.warning("bass_retune: could not parse %s: %s", hsan_path.name, exc)
                continue

            entries = data.get("Entries", {})
            source_key = None
            if arr_id in entries:
                source_key = arr_id
            else:
                arr_id_norm = arr_id.upper().replace("-", "")
                for k, v in entries.items():
                    if (k.upper().replace("-", "") == arr_id_norm
                            or v.get("Attributes", {}).get("ArrangementName") == "Bass"):
                        source_key = k
                        break

            if source_key is None:
                continue

            new_entry = copy.deepcopy(entries[source_key])
            attrs = new_entry.get("Attributes", {})
            attrs["Tuning"] = {f"string{i}": 0 for i in range(6)}
            attrs["ArrangementId"] = new_arr_id
            attrs["PersistentID"] = new_persistent_id
            attrs["ArrangementName"] = "Bass E-Std"
            # Rewrite SongXml/SongBin URNs to point at the new _bassestd SNG/XML.
            # Some CDLCs embed fully-qualified URNs in the hsan entries; without this
            # rewrite both Bass and Bass E-Std point at the same _bass SNG and the
            # game deduplicates them, making the E-standard arrangement invisible in
            # the path selector.  The `if key in attrs` guard makes this a no-op for
            # CDLCs that omit these fields, so existing working songs are unaffected.
            for key in ("SongXml", "SongBin"):
                if key in attrs:
                    attrs[key] = _replace_stem_ci(attrs[key], orig_urn_stem, new_urn_stem)

            entries[new_arr_id] = new_entry
            try:
                hsan_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.debug("bass_retune: added E-std entry to %s", hsan_path.name)
            except Exception as exc:
                log.warning("bass_retune: failed to write %s: %s", hsan_path.name, exc)

        # ── Patch xblock ──────────────────────────────────────────────────────
        for xblock_path in sorted(tmp_path.rglob("*.xblock")):
            try:
                ET.register_namespace("", "")
                tree = ET.parse(xblock_path)
                root = tree.getroot()
            except ET.ParseError as exc:
                log.warning("bass_retune: could not parse xblock %s: %s", xblock_path.name, exc)
                continue

            # Find the Bass entity: match by id attribute (GUID), then by URN content.
            bass_entity = None
            arr_id_norm = arr_id.upper().replace("-", "")
            for entity in root.iter("entity"):
                eid = entity.get("id", "").upper().replace("-", "")
                if eid == arr_id_norm:
                    bass_entity = entity
                    break

            if bass_entity is None:
                for entity in root.iter("entity"):
                    text_content = ET.tostring(entity, encoding="unicode").lower()
                    if orig_urn_stem in text_content and "estd" not in text_content:
                        bass_entity = entity
                        break

            if bass_entity is None:
                log.warning(
                    "bass_retune: could not find bass entity in %s — "
                    "arrangement may not appear in-game path selector",
                    xblock_path.name,
                )
                continue

            new_entity = copy.deepcopy(bass_entity)
            new_entity.set("id", new_arr_id)
            _rewrite_xblock_urns(new_entity, orig_urn_stem, new_urn_stem)

            parent = _find_parent(root, bass_entity)
            if parent is not None:
                idx = list(parent).index(bass_entity)
                parent.insert(idx + 1, new_entity)
                try:
                    tree.write(xblock_path, encoding="utf-8", xml_declaration=True)
                    log.info("bass_retune: patched xblock %s", xblock_path.name)
                except Exception as exc:
                    log.warning("bass_retune: failed to write xblock %s: %s", xblock_path.name, exc)
            else:
                log.warning("bass_retune: could not find parent element in %s", xblock_path.name)

        added[arr_info["song_key"] or new_urn_stem] = new_arr_id

    return added


def _rewrite_xblock_urns(element, old_stem: str, new_stem: str):
    """Recursively replace old_stem with new_stem in all XML attributes and text."""
    for attr, val in list(element.attrib.items()):
        if old_stem in val.lower():
            # Preserve case of the rest of the string; only replace the stem portion.
            element.set(attr, _replace_stem_ci(val, old_stem, new_stem))
    if element.text and old_stem in element.text.lower():
        element.text = _replace_stem_ci(element.text, old_stem, new_stem)
    for child in element:
        _rewrite_xblock_urns(child, old_stem, new_stem)


def _replace_stem_ci(text: str, old_stem: str, new_stem: str) -> str:
    """Case-insensitive replace of old_stem with new_stem in text."""
    import re as _re
    return _re.sub(_re.escape(old_stem), new_stem, text, flags=_re.IGNORECASE)


def _find_parent(root, target):
    """Return the parent element of target within the tree rooted at root."""
    for parent in root.iter():
        for child in parent:
            if child is target:
                return parent
    return None


# ── Sidecar flags cache ───────────────────────────────────────────────────────

import hashlib as _hashlib


def _flags_cache_dir() -> Path:
    """Return the flags_cache directory inside the plugin dir, creating it if absent."""
    d = Path(__file__).parent / "flags_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flags_cache_path(filename: str, arr_idx: int) -> Path:
    """Return the sidecar JSON path for a given filename + arrangement index."""
    sha = _hashlib.sha1(filename.encode("utf-8", errors="replace")).hexdigest()[:12]
    return _flags_cache_dir() / f"{sha}_{arr_idx}.json"


def _compute_flags_from_psarc(psarc_path: str):
    """Read bass_retune_flag attributes directly from the E-Std XML in a PSARC.

    Returns (arr_idx, {beat_index: flag_str}) or None if no E-Std arrangement
    found. Uses the XML directly rather than load_song so that bass_retune_flag
    attributes written during conversion are not lost through _parse_note.

    arr_idx is derived from the arrangement order in the manifest JSON so it
    matches the index the /flags endpoint will be queried with.
    """
    from psarc import unpack_psarc, read_psarc_entries
    import json as _json

    # Find the arrangement index of the E-Std bass from the manifest.
    manifest_files = read_psarc_entries(psarc_path, ["*.json"])
    arr_idx = None
    for path, data in sorted(manifest_files.items()):
        if not path.endswith(".json"):
            continue
        try:
            j = _json.loads(data)
        except Exception:
            continue
        entries = list(j.get("Entries", {}).values())
        for i, v in enumerate(entries):
            name = v.get("Attributes", {}).get("ArrangementName", "")
            if "estd" in name.lower() or "e-std" in name.lower() or "e std" in name.lower():
                arr_idx = i
                break
        if arr_idx is not None:
            break

    # Fall back to scanning by arrangement name in all manifests
    if arr_idx is None:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        unpack_psarc(psarc_path, tmp)
        tmp_path = Path(tmp)

        estd_xmls = [
            f for f in tmp_path.rglob("*.xml")
            if "estd" in f.stem.lower()
        ]
        if not estd_xmls:
            return None

        xml_path = sorted(estd_xmls)[0]
        root = ET.parse(xml_path).getroot()

        # Build time -> flag mapping from XML attributes
        time_flags: dict[float, str] = {}
        for tag in ("note", "chordNote"):
            for elem in root.iter(tag):
                flag = elem.get("bass_retune_flag", "")
                if not flag:
                    continue
                try:
                    t = float(elem.get("time", "-1"))
                except (TypeError, ValueError):
                    continue
                if t < 0:
                    continue
                existing = time_flags.get(t)
                if existing is None or flag == "octaveUp":
                    time_flags[t] = flag

        if not time_flags:
            return (arr_idx, {})

        # Map time -> beat index using rs2gp measure/event logic
        from rs2gp import _merge_events, _parse_measures, _fallback_measure
        from beat_index import compute_flags_map
        from song import load_song

        with tempfile.TemporaryDirectory() as tmp2:
            unpack_psarc(psarc_path, tmp2)
            song = load_song(tmp2)

        if not song or arr_idx >= len(song.arrangements):
            return (arr_idx, {})

        # arr_idx above is the manifest entry order -- it does NOT necessarily
        # match the slopsmith arrangement index (song.arrangements position),
        # which is what bundle.songInfo.arrangement_index reports and what the
        # /flags endpoint is queried with.  Find the E-Std arrangement by name
        # in song.arrangements so both the events and the cache slot are correct.
        slopsmith_idx = None
        for si, a in enumerate(song.arrangements):
            n = a.name.lower()
            if "estd" in n or "e-std" in n or "e std" in n or "bass e-std" in n:
                slopsmith_idx = si
                break
        if slopsmith_idx is not None:
            arr_idx = slopsmith_idx  # use slopsmith index for events + cache key

        if arr_idx >= len(song.arrangements):
            return (arr_idx, {})

        arr = song.arrangements[arr_idx]
        is_bass = "bass" in arr.name.lower()
        num_strings = 4 if is_bass else 6
        measures = _parse_measures(song.beats)
        if not measures:
            measures = [_fallback_measure(song.song_length)]

        # Build synthetic events with bass_retune_flag from the XML time map,
        # merging onto the note events that load_song produced.
        events = _merge_events(arr)
        for ev in events:
            t = ev.get("time", -1)
            if ev.get("type") == "chord":
                for nd in ev.get("chord_notes", []):
                    nd_t = nd.get("time", t)
                    nd["bass_retune_flag"] = time_flags.get(nd_t, "")
            else:
                ev["bass_retune_flag"] = time_flags.get(t, "")

        flag_map = compute_flags_map(events, measures, num_strings)
        return (arr_idx, flag_map)


def _write_flags_cache(filename: str, arr_idx: int, flag_map: dict) -> None:
    """Atomically write flag_map to the sidecar JSON file."""
    import json as _json
    path = _flags_cache_path(filename, arr_idx)
    tmp = path.with_suffix(".tmp")
    payload = {str(k): v for k, v in flag_map.items()}
    tmp.write_text(_json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _read_flags_cache(filename: str, arr_idx: int) -> dict | None:
    """Read the sidecar JSON, returning None if the file is absent or unreadable."""
    import json as _json
    path = _flags_cache_path(filename, arr_idx)
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, _json.JSONDecodeError):
        return None


# ── song.py patch helpers ─────────────────────────────────────────────────────

def _song_py_path() -> Path:
    """Resolve the path to song.py via Python's import system.

    Uses importlib to find song.py's actual location on disk rather than
    walking relative to __file__ — this is robust whether the core files
    live alongside the plugins (dev layout) or under Program Files (MSI
    install) as long as slopsmith's lib/ directory is on sys.path.
    """
    import importlib.util
    spec = importlib.util.find_spec("song")
    if spec and spec.origin:
        return Path(spec.origin)
    # Fallback: relative walk from this file (dev layout).
    return Path(__file__).parent.parent.parent / "lib" / "song.py"


def _check_song_py() -> bool:
    """Return True if bass_retune_flag field is present in song.py's Note class."""
    try:
        text = _song_py_path().read_text(encoding="utf-8", errors="replace")
        return "bass_retune_flag" in text
    except OSError:
        return False


def _check_cache_dir() -> bool:
    """Return True if the flags_cache directory is writable."""
    try:
        d = _flags_cache_dir()
        probe = d / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _backups_dir() -> Path:
    """Return the backups directory inside the plugin dir, creating it if absent."""
    d = Path(__file__).parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _patch_song_py() -> dict:
    """Insert bass_retune_flag field into song.py's Note dataclass if absent.

    Backs up the original song.py to plugins/bass-retune/backups/song.py.bak
    (stamped with a timestamp) before writing, so the unmodified core file is
    always recoverable.

    Finds the line ``    tap: bool = False`` and inserts the field immediately
    after it.  Returns a result dict with keys: patched, already_present, error,
    and backup_path (str | None).
    """
    import shutil as _shutil
    import datetime as _dt

    path = _song_py_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"patched": False, "already_present": False, "error": str(exc), "backup_path": None}

    if "bass_retune_flag" in text:
        return {"patched": False, "already_present": True, "error": None, "backup_path": None}

    ANCHOR = "    tap: bool = False\n"
    INSERT = "    bass_retune_flag: str = \"\"\n"

    if ANCHOR not in text:
        return {
            "patched": False,
            "already_present": False,
            "error": "insertion point not found — manual patch required",
            "backup_path": None,
        }

    # Back up the unmodified file before touching it.
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = _backups_dir() / f"song.py.{stamp}.bak"
    backup_path_str = str(backup_path)
    try:
        _shutil.copy2(str(path), str(backup_path))
        log.info("bass_retune: backed up %s → %s", path.name, backup_path)
    except OSError as exc:
        return {
            "patched": False,
            "already_present": False,
            "error": f"backup failed: {exc}",
            "backup_path": None,
        }

    new_text = text.replace(ANCHOR, ANCHOR + INSERT, 1)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return {"patched": False, "already_present": False, "error": str(exc), "backup_path": backup_path_str}

    return {"patched": True, "already_present": False, "error": None, "backup_path": backup_path_str}


# ── Route setup ───────────────────────────────────────────────────────────────

def setup(app, context):

    @app.get("/api/plugins/bass_retune/songs")
    def list_eligible_songs():
        """Scan the library and return all songs with non-E-standard bass arrangements
        that have not yet had an E-standard Bass arrangement added."""
        dlc_dir = context["get_dlc_dir"]()
        if not dlc_dir:
            return {"error": "DLC folder not configured", "songs": []}

        meta_db = context["meta_db"]

        try:
            rows = meta_db.conn.execute(
                "SELECT rowid, filename, title, artist, arrangements FROM songs"
            ).fetchall()
        except Exception as e:
            log.exception("bass_retune/songs: DB query failed")
            return {"error": str(e), "songs": []}

        results = []
        for rowid, filename, title, artist, arrangements_json in rows:
            try:
                arrangements = json.loads(arrangements_json) if arrangements_json else []
            except Exception:
                continue

            bass_arrangements = [a for a in arrangements if a.get("name") == "Bass"]
            if not bass_arrangements:
                continue

            # If the song already has a Bass E-Std arrangement it has already
            # been processed by this plugin — skip it.  The synchronous DB
            # update in convert() keeps this flag current; no PSARC re-read
            # is needed here.
            if any(a.get("name") == "Bass E-Std" for a in arrangements):
                continue

            psarc_path = dlc_dir / filename
            if not psarc_path.exists():
                continue
            try:
                arr_tunings = _get_bass_tuning_offsets(str(psarc_path))
            except Exception as e:
                log.warning("bass_retune/songs: failed to read tuning for %s: %s", filename, e)
                continue

            if not arr_tunings:
                continue

            for arr_info in arr_tunings.values():
                offsets = arr_info["offsets"]
                if offsets != [0, 0, 0, 0]:
                    results.append({
                        "filename": filename,
                        "title": title or filename,
                        "artist": artist or "",
                        "offsets": offsets,
                        "rowid": rowid,
                    })
                    break

        results.sort(key=lambda s: (s["title"] or "").lower())
        return {"songs": results}

    @app.get("/api/plugins/bass_retune/check")
    def check(filename: str):
        """Pre-flight scan: is this song eligible and are there any octave-up notes?"""
        dlc_dir = context["get_dlc_dir"]()
        if not dlc_dir:
            return {"eligible": False, "reason": "DLC folder not configured"}

        psarc_path = str(dlc_dir / filename)
        if not Path(psarc_path).exists():
            return {"eligible": False, "reason": "File not found"}

        # If the PSARC already has a BassEstd arrangement it has been processed.
        try:
            from psarc import read_psarc_entries
            files = read_psarc_entries(psarc_path, ["*.json"])
            for data in files.values():
                try:
                    j = json.loads(data)
                except Exception:
                    continue
                for v in j.get("Entries", {}).values():
                    if v.get("Attributes", {}).get("ArrangementName") == "Bass E-Std":
                        return {"eligible": False, "reason": "E-standard Bass arrangement already added"}
        except Exception:
            pass  # non-fatal; proceed to full scan

        try:
            return _scan_for_negatives(psarc_path, filename, log)
        except Exception as e:
            log.exception("bass_retune check failed for %s", filename)
            return {"eligible": False, "reason": str(e)}

    @app.get("/api/plugins/bass_retune/debug_layout")
    def debug_layout(filename: str):
        """Diagnose duplicate-arrangement bugs by exposing the internal PSARC
        layout and replicating song.py's exact _manifest_names resolution."""
        dlc_dir = context["get_dlc_dir"]()
        if not dlc_dir:
            return {"error": "DLC folder not configured"}

        psarc_path = str(dlc_dir / filename)
        if not Path(psarc_path).exists():
            return {"error": f"File not found: {filename}"}

        from psarc import unpack_psarc, read_psarc_entries

        # ── Lightweight pass: read JSON/hsan content without full unpack ──────
        manifest_data = []
        try:
            json_files = read_psarc_entries(psarc_path, ["*.json", "*.hsan"])
            for path, data in sorted(json_files.items()):
                try:
                    j = json.loads(data)
                    entries = {}
                    for k, v in j.get("Entries", {}).items():
                        attrs = v.get("Attributes", {})
                        entries[k] = {
                            "ArrangementName": attrs.get("ArrangementName"),
                            "SongXml":         attrs.get("SongXml"),
                            "SongBin":         attrs.get("SongBin"),
                            "PersistentID":    attrs.get("PersistentID"),
                        }
                    manifest_data.append({"file": path, "entries": entries})
                except Exception as e:
                    manifest_data.append({"file": path, "error": str(e)})
        except Exception as e:
            manifest_data.append({"error": f"read_psarc_entries failed: {e}"})

        # ── Full unpack: check XML/SNG layout and stem alignment ──────────────
        xml_files_info = []
        sng_files_info = []
        manifest_names = {}

        try:
            with tempfile.TemporaryDirectory() as tmp:
                unpack_psarc(psarc_path, tmp)
                tmp_path = Path(tmp)

                # Replicate song.py's exact _manifest_names build (lines 935-946)
                for jf in sorted(tmp_path.rglob("*.json")):
                    try:
                        data = json.loads(jf.read_text())
                        for k, v in (data.get("Entries") or {}).items():
                            attrs = v.get("Attributes") or {}
                            arr_name = attrs.get("ArrangementName", "")
                            if arr_name and arr_name not in ("Vocals", "ShowLights", "JVocals"):
                                prev = manifest_names.get(jf.stem.lower())
                                manifest_names[jf.stem.lower()] = arr_name
                                if prev and prev != arr_name:
                                    log.warning(
                                        "bass_retune debug: stem %r overwritten in "
                                        "_manifest_names: %r -> %r",
                                        jf.stem.lower(), prev, arr_name,
                                    )
                    except Exception:
                        continue

                # Check every arrangement XML against the lookup
                for f in sorted(tmp_path.rglob("*.xml")):
                    try:
                        root = ET.parse(f).getroot()
                        arr_el = root.find("arrangement")
                        arr_tag = (
                            arr_el.text.strip()
                            if arr_el is not None and arr_el.text
                            else "(no arrangement tag)"
                        )
                    except Exception:
                        arr_tag = "(parse error)"

                    stem = f.stem.lower()
                    lookup = manifest_names.get(stem)

                    # Replicate song.py's filename fallback (lines 1033-1045)
                    if lookup is None:
                        fname = stem
                        if "lead" in fname:
                            fallback = "Lead"
                        elif "rhythm" in fname:
                            fallback = "Rhythm"
                        elif "bass" in fname:
                            fallback = "Bass"
                        elif "combo" in fname:
                            fallback = "Combo"
                        else:
                            fallback = f.stem
                        resolution = f"MISS → fallback → '{fallback}'"
                    else:
                        resolution = f"OK → '{lookup}'"

                    xml_files_info.append({
                        "path":             str(f.relative_to(tmp_path)),
                        "stem":             f.stem,
                        "arrangement_tag":  arr_tag,
                        "manifest_lookup":  resolution,
                    })

                for f in sorted(tmp_path.rglob("*.sng")):
                    sng_files_info.append(str(f.relative_to(tmp_path)))

        except Exception as e:
            xml_files_info.append({"error": f"unpack failed: {e}"})

        return {
            "filename":              filename,
            "manifest_names_built":  manifest_names,
            "xml_files":             xml_files_info,
            "sng_files":             sng_files_info,
            "json_manifest_contents": manifest_data,
        }

    @app.get("/api/plugins/bass_retune/debug_flags_xml")
    def debug_flags_xml(filename: str):
        """Scan the E-Std bass XML inside a converted PSARC for bass_retune_flag
        attributes, confirming whether the flags were written during conversion.

        Returns a summary of flagged vs unflagged notes plus up to 10 samples
        of each flag value so the caller can verify the data is present before
        suspecting downstream rendering (rs2gp / alphaTab).
        """
        dlc_dir = context["get_dlc_dir"]()
        if not dlc_dir:
            return {"error": "DLC folder not configured"}

        psarc_path = str(dlc_dir / filename)
        if not Path(psarc_path).exists():
            return {"error": f"File not found: {filename}"}

        from psarc import unpack_psarc

        try:
            with tempfile.TemporaryDirectory() as tmp:
                unpack_psarc(psarc_path, tmp)
                tmp_path = Path(tmp)

                # Find the E-Std bass XML — identified by "estd" or "bassestd"
                # in the stem, falling back to any bass XML if none found.
                xml_candidates = list(tmp_path.rglob("*.xml"))
                estd_xmls = [
                    f for f in xml_candidates
                    if "estd" in f.stem.lower() and "bass" in f.stem.lower()
                ]
                if not estd_xmls:
                    # Broader fallback: any file with "estd" in the stem
                    estd_xmls = [f for f in xml_candidates if "estd" in f.stem.lower()]
                if not estd_xmls:
                    return {
                        "filename": filename,
                        "error": "No E-Std XML found in PSARC — has this song been converted yet?",
                        "all_xml_stems": [f.stem for f in xml_candidates],
                    }

                results = []
                for xml_path in sorted(estd_xmls):
                    try:
                        tree = ET.parse(xml_path)
                        root = tree.getroot()
                    except ET.ParseError as e:
                        results.append({"xml": xml_path.stem, "error": f"Parse error: {e}"})
                        continue

                    counts = {}   # flag_value -> int
                    samples = {}  # flag_value -> [list of sample dicts, max 10]
                    total = 0

                    for tag in ("note", "chordNote"):
                        for elem in root.iter(tag):
                            total += 1
                            flag = elem.get("bass_retune_flag", "")
                            counts[flag] = counts.get(flag, 0) + 1
                            if flag and len(samples.get(flag, [])) < 10:
                                samples.setdefault(flag, []).append({
                                    "tag":    tag,
                                    "time":   elem.get("time"),
                                    "string": elem.get("string"),
                                    "fret":   elem.get("fret"),
                                })

                    results.append({
                        "xml_file":           str(xml_path.relative_to(tmp_path)),
                        "total_notes":        total,
                        "flag_counts":        counts,
                        "flagged_octaveUp":   counts.get("octaveUp", 0),
                        "flagged_stringDown": counts.get("stringDown", 0),
                        "unflagged":          counts.get("", 0),
                        "samples":            samples,
                    })

                return {"filename": filename, "estd_arrangements": results}

        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"error": f"debug_flags failed: {e}"}

    @app.get("/api/plugins/bass_retune/rs2gp_path")
    def rs2gp_path():
        import rs2gp, inspect
        return {"path": inspect.getfile(rs2gp)}

    @app.get("/api/plugins/bass_retune/debug_song")
    def debug_song(filename: str):
        """Confirm which song.py is loaded and whether bass_retune_flag survives load_song."""
        import song as song_mod, inspect
        from song import load_song, Note
        song_path = inspect.getfile(song_mod)
        has_field = hasattr(Note, "__dataclass_fields__") and "bass_retune_flag" in Note.__dataclass_fields__

        dlc_dir = context["get_dlc_dir"]()
        if not dlc_dir:
            return {"song_path": song_path, "has_field": has_field, "error": "DLC folder not configured"}

        psarc_path = str(dlc_dir / filename)
        if not Path(psarc_path).exists():
            return {"song_path": song_path, "has_field": has_field, "error": "File not found"}

        try:
            from psarc import unpack_psarc
            with tempfile.TemporaryDirectory() as tmp:
                unpack_psarc(psarc_path, tmp)
                loaded = load_song(tmp)

            arr_names = [a.name for a in (loaded.arrangements if loaded else [])]
            flagged = 0
            sample = []
            for arr in (loaded.arrangements if loaded else []):
                if "estd" in arr.name.lower() or "e-std" in arr.name.lower():
                    for n in arr.notes:
                        f = getattr(n, "bass_retune_flag", None)
                        if f:
                            flagged += 1
                            if len(sample) < 5:
                                sample.append({"flag": f, "string": n.string, "fret": n.fret})
            return {
                "song_path": song_path,
                "has_field": has_field,
                "arrangements": arr_names,
                "flagged_notes_in_estd": flagged,
                "sample": sample,
            }
        except Exception as e:
            import traceback; traceback.print_exc()
            return {"song_path": song_path, "has_field": has_field, "error": str(e)}

    @app.get("/api/plugins/bass_retune/flags")
    def get_flags(filename: str, arrangement: int = 0):
        """Return a beat-index → flag map for the E-Std bass arrangement.

        Reads from the sidecar JSON cache written by convert() — no PSARC
        unpack or GP5 conversion is performed at request time.

        Response shape:
            {"flags": {"3": "stringDown", "17": "octaveUp", ...}}

        An empty ``flags`` dict means the sidecar is absent (song not yet
        converted, or arrangement has no retune-flagged notes).
        """
        data = _read_flags_cache(filename, arrangement)
        if data is None:
            return {"flags": {}}
        return {"flags": data}

    @app.get("/api/plugins/bass_retune/health")
    def health():
        """Return readiness checks for the two durable dependencies."""
        try:
            import ctypes
            is_elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_elevated = False
        return {
            "song_py_ok": _check_song_py(),
            "cache_dir_ok": _check_cache_dir(),
            "is_elevated": is_elevated,
        }

    @app.post("/api/plugins/bass_retune/patch_core")
    def patch_core():
        """Insert bass_retune_flag into song.py's Note dataclass if absent."""
        return _patch_song_py()
    @app.post("/api/plugins/bass_retune/convert")
    def convert(body: dict):
        """Add an E-standard Bass arrangement alongside the original and rewrite the PSARC.

        The original Bass arrangement is left completely untouched. A second Bass
        arrangement (E standard) is added, sharing the same audio, art, and song
        metadata. In-game the player can select between e.g. "Bass (Drop D)" and
        "Bass (E Standard)" the same way Lead 1 / Lead 2 path selection works.
        """
        filename = body.get("filename", "")

        dlc_dir = context["get_dlc_dir"]()
        if not dlc_dir:
            return {"success": False, "error": "DLC folder not configured"}

        psarc_path = str(dlc_dir / filename)
        if not Path(psarc_path).exists():
            return {"success": False, "error": "File not found"}


        try:
            arr_tunings = _get_bass_tuning_offsets(psarc_path)
            if not arr_tunings:
                return {"success": False, "error": "No bass arrangements found"}

            # Only process arrangements that actually need shifting
            to_shift = {k: v for k, v in arr_tunings.items()
                        if v["offsets"] != [0, 0, 0, 0]}
            if not to_shift:
                return {"success": False, "error": "Bass arrangement is already E standard"}

            # Deduplicate by song_key: multiple arr_ids can reference the same
            # physical SNG/XML (duplicate manifest entries, common in some CDLCs).
            # Processing the same file twice causes a WinError 32 file lock on
            # the second iteration. Keep the entry whose offsets are closest to
            # zero (smallest total absolute offset) — that is the most plausible
            # tuning. Genuine multi-bass CDLCs use distinct song_keys and are
            # unaffected (each song_key gets its own output SNG).
            _seen_keys = {}
            for _arr_id, _arr_info in to_shift.items():
                _sk = _arr_info["song_key"]
                _score = sum(abs(o) for o in _arr_info["offsets"])
                if _sk not in _seen_keys or _score < _seen_keys[_sk][0]:
                    _seen_keys[_sk] = (_score, _arr_id, _arr_info)
                else:
                    log.warning(
                        "bass_retune: skipping duplicate song_key %r (arr_id=%s, offsets=%s) "
                        "-- kept entry with smaller offset distance",
                        _sk, _arr_id, _arr_info["offsets"],
                    )
            to_shift = {v[1]: v[2] for v in _seen_keys.values()}

            total_stats = {
                "shifted_per_string": {},
                "octave_up": 0,
                "octave_up_sections": [],
                "unchanged": 0,
                "errors": [],
            }

            with tempfile.TemporaryDirectory() as tmp:
                from psarc import unpack_psarc
                from patcher import pack_psarc
                unpack_psarc(psarc_path, tmp)
                tmp_path = Path(tmp)

                for arr_id, arr_info in to_shift.items():
                    offsets = arr_info["offsets"]
                    song_key = arr_info["song_key"]

                    # delta = how many semitones to ADD to each fret.
                    # offset is the tuning delta from E standard (negative = tuned down).
                    # The tab was written with frets shifted to compensate for the altered
                    # open string, so applying the offset directly corrects back to
                    # E standard fingering.
                    # Drop D: string0 offset=-2 → delta=-2 (fret moves down 2)
                    deltas = list(offsets)

                    # Locate or decompile the source XML for this arrangement.
                    xml_path, decompiled_from_sng = _get_xml_for_arrangement(
                        tmp_path, "Bass", song_key, log
                    )
                    if not xml_path:
                        if not RSCLI.exists():
                            total_stats["errors"].append(
                                f"rscli not found at {RSCLI} — cannot decompile SNG for "
                                f"arrangement {song_key}. Install rscli to enable SNG-only CDLC support."
                            )
                        else:
                            total_stats["errors"].append(
                                f"Could not locate or decompile Bass arrangement for {song_key}"
                            )
                        continue

                    # ── Build the E-standard SNG ──────────────────────────────
                    # We need a *copy* of the XML to shift so the original bass
                    # XML (and therefore its SNG) remains untouched.  The new
                    # SNG will be named {song_key}_bassestd.sng and sit next to
                    # the original in the same songs/bin/generic/ directory.
                    #
                    # IMPORTANT: estd_xml must be named {song_key}_bassestd.xml —
                    # the same stem as the JSON written by _add_estd_bass_arrangement
                    # ({song_key}_bassestd.json).  song.py builds _manifest_names by
                    # keying each JSON by its filename stem, then looks up each XML
                    # by its stem.  If the XML stem doesn't match the JSON stem,
                    # song.py falls back to the filename heuristic ("bass" in fname
                    # → "Bass") and the arrangement shows up as a duplicate "Bass"
                    # instead of "Bass E-Std".
                    import shutil as _shutil
                    _estd_stem = f"{song_key.lower()}_bassestd" if song_key else (
                        xml_path.stem.lower().replace("_bass", "_bassestd")
                    )

                    # When xml_path was decompiled from SNG it lives in
                    # songs/bin/generic/ — a directory whose XMLs have no
                    # <arrangement> element, so load_song() falls back to the
                    # filename heuristic and resolves "bassestd" → "Bass"
                    # (phantom duplicate) instead of "Bass E-Std".
                    # Place estd_xml in songs/arr/ (the canonical XML directory)
                    # so the manifest-stem lookup path is used instead.
                    if decompiled_from_sng:
                        _arr_dir = xml_path.parent.parent.parent / "songs" / "arr"
                        # Walk up until we find the songs/ directory.
                        _p = xml_path.parent
                        while _p != _p.parent:
                            if _p.name == "songs":
                                _arr_dir = _p / "arr"
                                break
                            _p = _p.parent
                        _arr_dir.mkdir(parents=True, exist_ok=True)
                        estd_xml = _arr_dir / (_estd_stem + ".xml")
                    else:
                        estd_xml = xml_path.with_name(_estd_stem + ".xml")
                    _shutil.copy2(str(xml_path), str(estd_xml))

                    # If we decompiled from SNG, delete the temporary working-copy
                    # XML that _get_xml_for_arrangement wrote into songs/bin/generic/.
                    # estd_xml was placed in songs/arr/ above, so _decompile_sng_only_arrangements
                    # will decompile the original bass SNG → songs/arr/billytryhon_bass.xml
                    # so load_song()'s XML-only path sees both Bass and Bass E-Std.
                    if decompiled_from_sng:
                        xml_path.unlink(missing_ok=True)

                    log.info(
                        "bass_retune: shifting copy %s with deltas %s",
                        estd_xml.name, deltas,
                    )
                    stats = _shift_frets_in_xml(estd_xml, deltas)

                    # Merge stats
                    for si, count in stats["shifted_per_string"].items():
                        total_stats["shifted_per_string"][si] = (
                            total_stats["shifted_per_string"].get(si, 0) + count
                        )
                    total_stats["octave_up"] += stats["octave_up"]
                    seen = set(total_stats["octave_up_sections"])
                    for sec in stats["octave_up_sections"]:
                        if sec not in seen:
                            seen.add(sec)
                            total_stats["octave_up_sections"].append(sec)
                    total_stats["unchanged"] += stats["unchanged"]
                    total_stats["errors"].extend(stats["errors"])

                    # Compile the shifted XML to an E-standard SNG.
                    # Locate the original SNG so we know what directory to land in.
                    orig_sng = _find_arrangement_sng(tmp_path, "Bass", song_key)
                    if orig_sng:
                        estd_sng = orig_sng.parent / f"{song_key.lower()}_bassestd.sng"
                        if RSCLI.exists():
                            log.info("bass_retune: compiling %s → %s", estd_xml.name, estd_sng.name)
                            if not _xml_to_sng(estd_xml, estd_sng):
                                # rscli compile failed — fall back to copying the original
                                # SNG and letting the manifest/xblock point to the copy.
                                # The tab will sound slightly wrong but the arrangement
                                # will at least load; we surface a warning.
                                log.warning(
                                    "bass_retune: xml2sng failed for %s, copying original SNG as fallback",
                                    estd_xml.name,
                                )
                                _shutil.copy2(str(orig_sng), str(estd_sng))
                                total_stats["errors"].append(
                                    f"xml2sng failed for {estd_xml.name} — "
                                    f"E-standard arrangement uses a copy of the original SNG "
                                    f"(tab will not reflect fret changes in-game)"
                                )
                        else:
                            # No rscli: copy the original SNG as a stand-in.
                            # The arrangement will appear selectable in-game but the
                            # tab fret positions won't be updated until rscli is available.
                            _shutil.copy2(str(orig_sng), str(estd_sng))
                            total_stats["errors"].append(
                                f"rscli not found — E-standard SNG for {song_key} is a copy of "
                                f"the original; install rscli for correct fret positions in-game"
                            )
                        log.info("bass_retune: E-standard SNG ready: %s", estd_sng.name)
                    else:
                        log.warning(
                            "bass_retune: no original SNG found for %s — skipping SNG creation",
                            song_key,
                        )

                    # NOTE: estd_xml is intentionally kept in the PSARC even after
                    # SNG compilation. load_song() in song.py scans for XML files to
                    # discover arrangements; if any XML exists in the directory it skips
                    # decompiling SNGs entirely (has_arrangement_xml guard). Without
                    # songkey_bassestd.xml present, the Bass E-Std arrangement is
                    # invisible in the player even though the SNG and metadata are correct.

                # Wire the new arrangement into manifests, hsan, and xblock.
                added = _add_estd_bass_arrangement(tmp_path, to_shift, log)

                # Scrub Bass/Lead/Rhythm entries from the hsan aggregate manifest
                # so song.py doesn't double-count them against the individual JSON
                # files.  Must run AFTER _add_estd_bass_arrangement so the Bass E-Std
                # entry it just wrote is protected by KEEP_IN_HSAN before the prune.
                _remove_non_bass_arrangements(tmp_path, log)

                if not added:
                    log.warning(
                        "bass_retune: _add_estd_bass_arrangement returned empty for %s — "
                        "E-standard arrangement may not appear in-game path selector",
                        filename,
                    )
                    total_stats["errors"].append(
                        "Could not fully wire E-standard arrangement into PSARC metadata — "
                        "the SNG was created but may not be selectable in-game"
                    )

                # Ensure every SNG in the output has a paired XML.
                #
                # song.py short-circuits SNG decompilation the moment any
                # non-infrastructure XML exists (has_arrangement_xml guard).
                # estd_xml is intentionally kept in the PSARC (so Bass E-Std
                # is discoverable), which means that guard is always True for
                # converted songs.  Any SNG that lacks a paired XML will be
                # invisible in the player.  Decompile them now so song.py's
                # XML-only path sees the full arrangement list.
                _decompile_sng_only_arrangements(tmp_path, log)

                # Overwrite the original PSARC with the augmented version.
                # Using a temp file + atomic rename so a pack failure doesn't
                # corrupt the original.
                tmp_out = Path(psarc_path + ".tmp")
                try:
                    pack_psarc(tmp, str(tmp_out))
                    tmp_out.replace(Path(psarc_path))
                    log.info("bass_retune: updated %s in-place", filename)
                except Exception:
                    tmp_out.unlink(missing_ok=True)
                    raise

            # ── Invalidate player extraction cache ───────────────────────
            # _get_or_extract() in server.py caches the unpacked PSARC for
            # 5 minutes by filename.  Without this the player would serve
            # stale pre-conversion content until the TTL expires, making
            # the new arrangement invisible until a restart.
            try:
                import server as _server
                with _server._extract_cache_lock:
                    old_entry = _server._extract_cache.pop(filename, None)
                if old_entry:
                    import shutil as _shutil
                    _shutil.rmtree(old_entry[0], ignore_errors=True)
                    log.info("bass_retune: evicted player cache for %s", filename)
            except Exception:
                log.warning("bass_retune: could not evict player cache for %s", filename, exc_info=True)

            # ── Update DB cache synchronously ─────────────────────────────
            # Rather than re-reading the entire PSARC from disk (which can
            # fail and trigger the delete-then-kick fallback, leaving the
            # library blank until the background scanner finishes), we read
            # the existing DB row and surgically append the new "Bass E-Std"
            # arrangement entry.  Everything else in the row — title, artist,
            # tuning, duration, etc. — is still correct; only arrangements
            # needs updating.  We also update mtime+size so the scanner's
            # freshness check (filename, mtime, size) treats this row as
            # current and skips re-indexing the file on its next pass.
            try:
                import server as _server
                psarc_file = Path(psarc_path)
                mtime, size = _server._stat_for_cache(psarc_file)
                meta_db = context["meta_db"]

                # Fetch the existing cached row so we can reuse all its fields.
                # Must hold _lock for the read: meta_db uses a single shared
                # connection with check_same_thread=False, and a bare .execute()
                # from this thread while the background scanner is iterating its
                # own cursor on the same connection can corrupt the scanner's
                # cursor state, causing it to write empty arrangements to songs
                # it was mid-processing.
                with meta_db._lock:
                    existing = meta_db.conn.execute(
                        "SELECT title, artist, album, year, duration, tuning, arrangements, "
                        "has_lyrics, format, stem_count, stem_ids, tuning_name, tuning_sort_key "
                        "FROM songs WHERE filename = ?",
                        (filename,)
                    ).fetchone()

                if existing is None:
                    # Row not present at all — fall back to a full re-read.
                    raise LookupError("no existing DB row for %s" % filename)

                (title, artist, album, year, duration, tuning, arrangements_json,
                 has_lyrics, fmt, stem_count, stem_ids_json,
                 tuning_name_val, tuning_sort_key) = existing

                arrangements = json.loads(arrangements_json) if arrangements_json else []

                # Append the new arrangement only if it isn't already recorded.
                # (Idempotent so a second call after a partial failure is safe.)
                if not any(a.get("name") == "Bass E-Std" for a in arrangements):
                    next_index = max((a.get("index", 0) for a in arrangements), default=-1) + 1
                    arrangements.append({"index": next_index, "name": "Bass E-Std", "notes": 0})

                meta = {
                    "title": title or "",
                    "artist": artist or "",
                    "album": album or "",
                    "year": year or "",
                    "duration": duration or 0.0,
                    "tuning": tuning or "",
                    "arrangements": arrangements,
                    "has_lyrics": bool(has_lyrics),
                    "format": fmt or "psarc",
                    "stem_count": int(stem_count or 0),
                    "stem_ids": json.loads(stem_ids_json) if stem_ids_json else [],
                    "tuning_name": tuning_name_val or "",
                    "tuning_sort_key": int(tuning_sort_key or 0),
                }
                meta_db.put(filename, mtime, size, meta)
                log.info("bass_retune: DB cache updated synchronously for %s", filename)
            except Exception:
                # Non-fatal: kick the background scanner so it re-indexes on
                # its next pass.  Do NOT delete the row — leaving the stale
                # row in place means the song remains visible in the library
                # (without the new tag) rather than disappearing until the
                # scan completes.
                log.warning("bass_retune: synchronous DB update failed for %s", filename, exc_info=True)

            # ── Write flags sidecar for tab view coloring ─────────────────
            # Load the converted PSARC, find the Bass E-Std arrangement, run
            # rocksmith_to_gp5 to obtain the beat-index → flag map, and write
            # it to flags_cache/ so the /flags endpoint can serve it instantly
            # without re-unpacking the PSARC on every tab-view request.
            try:
                _fc_flags = _compute_flags_from_psarc(psarc_path)
                if _fc_flags is not None:
                    arr_idx, flag_map = _fc_flags
                    _write_flags_cache(filename, arr_idx, flag_map)
                    log.info(
                        "bass_retune: wrote flags sidecar for %s arr=%d (%d flags)",
                        filename, arr_idx, len(flag_map),
                    )
            except Exception:
                log.warning(
                    "bass_retune: could not write flags cache for %s", filename, exc_info=True
                )

            # Build human-readable strings summary
            shifted_strings = [
                STRING_NAMES[si]
                for si in sorted(total_stats["shifted_per_string"].keys())
                if total_stats["shifted_per_string"][si] > 0
            ]

            return {
                "success": True,
                "new_filename": filename,  # same file, modified in-place
                "shifted_strings": shifted_strings,
                "octave_up_notes": total_stats["octave_up"],
                "octave_up_sections": total_stats["octave_up_sections"],
                "warnings": total_stats["errors"],
            }

        except Exception as e:
            log.exception("bass_retune convert failed for %s", filename)
            return {"success": False, "error": str(e)}

    @app.get("/api/plugins/bass_retune/debug_flags")
    def debug_flags(filename: str, arrangement: int = 0):
        dlc_dir = context["get_dlc_dir"]()
        psarc_path = str(dlc_dir / filename)
        import traceback
        try:
            result = _compute_flags_from_psarc(psarc_path)
            if result is None:
                return {"error": "No E-Std arrangement found"}
            arr_idx, flag_map = result
            _write_flags_cache(filename, arr_idx, flag_map)
            return {"flags_count": len(flag_map), "sample": dict(list(flag_map.items())[:10]), "arr_idx": arr_idx, "cache_written": True}
        except Exception:
            return {"error": traceback.format_exc()}

    @app.get("/api/plugins/bass_retune/debug_beatcount")
    def debug_beatcount(filename: str, arrangement: int = 0):
        from psarc import unpack_psarc
        import guitarpro
        import io
        import urllib.request
        with tempfile.TemporaryDirectory() as tmp:
            unpack_psarc(str(context["get_dlc_dir"]() / filename), tmp)
            tmp_path = Path(tmp)
            xml_candidates = list(tmp_path.rglob("*.xml"))
            estd_xmls = sorted([f for f in xml_candidates if "estd" in f.stem.lower()])
            ebeat_count = 0
            ebeat_sample = []
            ebeat_measure_attrs = []
            if estd_xmls:
                root = ET.parse(estd_xmls[0]).getroot()
                for eb in root.iter("ebeat"):
                    ebeat_count += 1
                    if len(ebeat_sample) < 10:
                        ebeat_sample.append(float(eb.get("time", -1)))
                        ebeat_measure_attrs.append(eb.get("measure", "?"))
        gp5_url = f"http://127.0.0.1:18000/api/plugins/tabview/gp5/{filename}?arrangement={arrangement}"
        with urllib.request.urlopen(gp5_url) as r:
            gp5_bytes = r.read()
        song = guitarpro.parse(io.BytesIO(gp5_bytes))
        track = song.tracks[0]
        total_gp5_beats = sum(len(m.voices[0].beats) for m in track.measures)
        beats_per_measure = [len(m.voices[0].beats) for m in track.measures]
        return {
            "ebeat_count": ebeat_count,
            "ebeat_sample": ebeat_sample,
            "ebeat_measure_attrs": ebeat_measure_attrs,
            "gp5_total_beats": total_gp5_beats,
            "gp5_measures": len(track.measures),
            "beats_per_measure": beats_per_measure[:20],
        }
