# Bass Retune - Slopsmith Plugin

Adds ability to append a selectable **E-standard bass arrangement (E A D G)** inside any PSARC whose bass track was recorded in an alternate tuning. The original arrangement remains in the file and is still selectable. Audio is never touched.

## What it does

Reads the per-string tuning offsets from the PSARC manifest, computes the fret deltas for E standard pitch, and rewrites all note and chord frets in a copy of the bass arrangement. The alternate version is registered as **"Bass E-Std"** — a new selectable arrangement inside the same PSARC file.

**Non-uniform tuning support.** Handles Drop D and any other tuning where strings have different semitone offsets by applying a per-string delta rather than a single uniform shift.

**Negative-fret fallback.** When a transposed note would land at a negative fret, the plugin tries to remap it to the next lower string at the same pitch (fret += 5) before falling back to an octave-up remap on the next higher string. The lower-string path is always tried first because it keeps the note in the same register. Notes that required an octave jump are flagged.

**SNG-only CDLC support.** CDLCs that ship without a source XML are decompiled via rscli, shifted, and recompiled back before repacking. The intermediate XML is not kept.

**Original arrangement preserved.** The original bass arrangement stays in the PSARC and can still be selected at any time.

---

## Tab View coloring

When **Bass E-Std** is open in Tab View, notes that were remapped during conversion are highlighted:

| Color | Meaning |
|---|---|
| Amber | Note moved up one octave to fit E standard |
| Cyan | Note remapped to a lower string at the same pitch |

This makes it immediately clear where the arrangement diverges from the original chart and what kind of remap happened.

Coloring requires a one-time patch to core's song.py that adds bass_retune_flag: str = "" as a field on the Note dataclass — something core doesn't currently have. The plugin's header bar will prompt for this on first use. Before writing, the original song.py is backed up to plugins/bass-retune/backups/song.py.<timestamp>.bak. The patch requires Slopsmith to be running with admin privileges on Windows to write to the install directory.

Conversions work without the patch — frets are shifted correctly and the arrangement is fully playable — but the amber/cyan highlighting will not appear in Tab View until it is applied.

If Slopsmith is updated and song.py is replaced, the patch will need to be reapplied. The plugin detects this automatically at startup and the prompt will reappear.

---

## Pre-flight warning

Before committing to a conversion, the modal runs a live analysis and reports how many notes will require an octave-up remap and which song sections they appear in. Lower-string remaps (same pitch, no register change) are not counted as warnings. You can review and cancel before anything is written.

---

## File structure

```
bass_retune/
├── plugin.json       Plugin manifest
├── routes.py         Backend — API endpoints and conversion logic
├── screen.html       Frontend HTML (song list, modal, status strip)
├── screen.js         Frontend JS (song list, modal, Tab View coloring)
└── beat_index.py     Beat-index computation for Tab View flag placement
```

---

## Known Limitations

**Chord templates.** The `chordTemplate` element uses `fret0..fret3` in reverse string order. The plugin accounts for this, but when a chord template fret goes negative and can't be string-remapped within the fixed template structure, that string is dropped from the chord (set to `-1`) rather than clamped to fret 0, which would play the wrong pitch.
