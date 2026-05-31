#!/usr/bin/env python3
"""Convert a monophonic MIDI into a rosintune score JSON.

Two rhythm modes:
  --mode uniform   every note is --unit (default eighth); final note uses
                   --final-duration. Exact for uniform-rhythm studies (e.g. #1).
  --mode quantize  (default) snap each note's inter-onset interval to the nearest
                   standard note value, relative to a detected base unit. Handles
                   mixed rhythms (dotted-eighth, 16th, 32nd) and basic triplets.
                   Rhythm is approximate on rubato performances - verify output.

Metadata: pass --key auto to detect the key from the pitch-class histogram.

Durations emitted: whole, dotted-half, half, dotted-quarter, quarter,
dotted-eighth, eighth, dotted-16th, 16th, 32nd, plus tuplet groups via a
`tuplet` field {count, inSpaceOf, base}. The iOS renderer must support these.
"""
import argparse
import json
import os
import statistics
import sys

import parse_midi as pm

# note value (beats) -> JSON token.  Order matters for nearest-snap search.
DUR_TOKENS = [
    (4.0, "whole"), (3.0, "dotted-half"), (2.0, "half"), (1.5, "dotted-quarter"),
    (1.0, "quarter"), (0.75, "dotted-eighth"), (0.5, "eighth"),
    (0.375, "dotted-16th"), (0.25, "16th"), (0.125, "32nd"),
]
BEATS_OF = {tok: b for b, tok in DUR_TOKENS}

VIOLIN_FIRST_POS = {
    "G3": (4, "0"), "G#3": (4, "1"), "A3": (4, "1"), "A#3": (4, "2"), "B3": (4, "2"),
    "C4": (4, "3"), "C#4": (4, "3"), "D4": (3, "0"), "D#4": (3, "1"), "E4": (3, "1"),
    "F4": (3, "2"), "F#4": (3, "2"), "G4": (3, "3"), "G#4": (3, "3"),
    "A4": (2, "0"), "A#4": (2, "1"), "B4": (2, "1"), "C5": (2, "2"), "C#5": (2, "2"),
    "D5": (2, "3"), "D#5": (2, "3"), "E5": (1, "0"), "F5": (1, "1"), "F#5": (1, "1"),
    "G5": (1, "2"), "G#5": (1, "2"), "A5": (1, "3"), "A#5": (1, "3"),
    "B5": (1, "4"), "C6": (1, "4"), "C#6": (1, "4"), "D6": (1, "4"),
}

MIDDLE_LINE = 71  # B4 in treble: at/above => stem down

# Krumhansl-Schmuckler key profiles
MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
# preferred spelling of each tonic pitch class for key names
SHARP_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


# ── MIDI extraction ────────────────────────────────────────────────
def extract_notes(midi_path):
    """Return (division, notes, dropped) where notes is a monophonic top-voice
    line [(onset_tick, pitch_name, midi, dur_ticks), ...].

    Double-stops/chords (simultaneous note-ons) are collapsed to the highest
    pitch; `dropped` counts the lower notes discarded (the JSON is single-voice).
    """
    div, events = pm.parse(midi_path)
    ons = sorted([e for e in events if e[4] == "on"])
    offs = sorted([e for e in events if e[4] == "off"])
    # collapse simultaneous onsets -> keep highest midi at each tick
    top = {}  # tick -> (tick, ti, p, v, k)
    dropped = 0
    for e in ons:
        t, ti, p = e[0], e[1], e[2]
        if t in top:
            dropped += 1
            if p > top[t][2]:
                top[t] = e
        else:
            top[t] = e
    notes = []
    used = [False] * len(offs)
    for (t, ti, p, v, _k) in sorted(top.values()):
        dur = None
        for oi, (ot, oti, op, ov, _ok) in enumerate(offs):
            if not used[oi] and op == p and ot >= t:
                dur = ot - t
                used[oi] = True
                break
        notes.append((t, pm.name(p), p, dur))
    return div, notes, dropped


# ── key detection ──────────────────────────────────────────────────
def detect_key(notes, division):
    """Krumhansl-Schmuckler, biased toward the final note as tonic (studies
    almost always cadence on the tonic), which resolves close keys (e.g. C vs G)."""
    weights = [0.0] * 12
    for (_t, _name, midi, dur) in notes:
        weights[midi % 12] += (dur or division) / division
    last_pc = notes[-1][2] % 12 if notes else None
    first_pc = notes[0][2] % 12 if notes else None
    ranked = []
    for tonic in range(12):
        for profile, mode in ((MAJOR_PROFILE, "Major"), (MINOR_PROFILE, "Minor")):
            rotated = [profile[(i - tonic) % 12] for i in range(12)]
            score = _corr(weights, rotated)
            if tonic == last_pc:
                score += 0.20          # strong: ends on this tonic
            if tonic == first_pc:
                score += 0.05          # weak: starts on this tonic
            ranked.append((score, tonic, mode))
    ranked.sort(reverse=True)
    _s, tonic, mode = ranked[0]
    return f"{SHARP_NAMES[tonic]} {mode}"


def _corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    den = (sum((a[i] - ma) ** 2 for i in range(n)) * sum((b[i] - mb) ** 2 for i in range(n))) ** 0.5
    return num / den if den else 0.0


# ── rhythm quantization ────────────────────────────────────────────
def snap_beats(raw_beats):
    """Nearest standard note value (token, beats)."""
    return min(DUR_TOKENS, key=lambda bt: abs(bt[0] - raw_beats))


def quantize_durations(notes, division, unit_beats):
    """Return list of (pitch, midi, beats, tuplet) using relative IOI snapping.

    A tuplet is (count, in_space_of, base_token) or None.
    """
    iois = [notes[i + 1][0] - notes[i][0] for i in range(len(notes) - 1)]
    iois = [x for x in iois if x > 0]
    base = statistics.median(iois) if iois else division  # ticks per --unit
    out = []
    i = 0
    n = len(notes)
    while i < n:
        # inter-onset for all but the last note; last uses its note-off / unit
        if i < n - 1:
            ioi = notes[i + 1][0] - notes[i][0]
        else:
            ioi = notes[i][3] or base
        ratio = ioi / base if base else 1.0  # in units of --unit

        # triplet detection: three near-equal onsets that together span ~2 units
        trip = _try_triplet(notes, i, base)
        if trip:
            base_tok = snap_beats(unit_beats)[1]
            for k in range(3):
                pitch, midi = notes[i + k][1], notes[i + k][2]
                out.append((pitch, midi, unit_beats * 2.0 / 3.0,
                            (3, 2, base_tok)))
            i += 3
            continue

        tok_beats, _tok = snap_beats(ratio * unit_beats)
        out.append((notes[i][1], notes[i][2], tok_beats, None))
        i += 1
    return out, base


def _try_triplet(notes, i, base):
    """True if notes[i..i+2] look like an even triplet (~2/3 unit each)."""
    if i + 2 >= len(notes):
        return False
    g = [notes[j + 1][0] - notes[j][0] for j in (i, i + 1)]
    g.append(notes[i + 2][3] or g[-1])
    if any(x <= 0 for x in g):
        return False
    target = base * 2.0 / 3.0
    return all(abs(x - target) / target < 0.18 for x in g[:2])


# ── note/measure assembly ──────────────────────────────────────────
def note_obj(pitch, midi, beat, beats, bowing, fmap, tuplet):
    string = fing = None
    if pitch is not None and fmap is not None:
        sf = fmap.get(pitch)
        if sf:
            string, fing = sf
    tok = snap_beats(beats)[1] if pitch is not None else _rest_token(beats)
    obj = {
        "pitch": pitch,
        "duration": tok,
        "beat": round(beat, 4),
        "show": None,
        "stem": (None if pitch is None else ("down" if midi >= MIDDLE_LINE else "up")),
        "flags": 0,
        "string": string,
        "fingering": fing,
        "bowing": (None if pitch is None else bowing),
        "articulation": None,
        "beam": None,
        "slur": None,
        "tie": None,
    }
    if tuplet is not None:
        obj["tuplet"] = {"count": tuplet[0], "inSpaceOf": tuplet[1], "base": tuplet[2]}
    return obj


def _rest_token(beats):
    return snap_beats(beats)[1]


def beats_to_rests(beat, gap):
    rests = []
    for value, tok in DUR_TOKENS:
        while gap >= value - 1e-6:
            rests.append((beat, value))
            beat += value
            gap -= value
    return rests


def effective_beats(entry):
    pitch, midi, beats, tuplet = entry
    return beats  # already adjusted for tuplet in quantize


def apply_beams(notes, group_beats):
    run = []

    def flush():
        if len(run) >= 2:
            run[0]["beam"] = "start"
            run[-1]["beam"] = "end"
            for x in run[1:-1]:
                x["beam"] = None
        run.clear()

    for nobj in notes:
        beamable = nobj["pitch"] is not None and BEATS_OF.get(nobj["duration"], 9) <= 0.5
        if run and (not beamable or
                    int(nobj["beat"] // group_beats) != int(run[-1]["beat"] // group_beats)):
            flush()
        if beamable:
            run.append(nobj)
        else:
            flush()
    flush()


def build(entries, args, detected_key):
    bpm = args.numerator * (4.0 / args.denominator)
    group_beats = bpm / 2.0
    fmap = None if args.no_fingering else VIOLIN_FIRST_POS

    dynamics = {}
    if args.dynamics:
        for pair in args.dynamics.split(","):
            m, text = pair.split(":", 1)
            dynamics.setdefault(int(m), []).append(text.strip())

    # place on a continuous timeline
    placed = []
    cum = 0.0
    for idx, e in enumerate(entries):
        pitch, midi, beats, tuplet = e
        if idx == len(entries) - 1 and args.final_duration:
            beats = BEATS_OF[args.final_duration]
            tuplet = None
        mi = int(cum // bpm + 1e-9)
        placed.append((mi, cum - mi * bpm, pitch, midi, beats, tuplet))
        cum += beats

    n_measures = placed[-1][0] + 1
    measures = []
    for mi in range(n_measures):
        notes, bow, filled = [], "down", 0.0
        for (m, bim, pitch, midi, beats, tuplet) in placed:
            if m != mi:
                continue
            notes.append(note_obj(pitch, midi, bim, beats, bow, fmap, tuplet))
            bow = "up" if bow == "down" else "down"
            filled = bim + beats
        gap = bpm - filled
        if gap > 1e-6:
            for (rbeat, rbeats) in beats_to_rests(filled, gap):
                notes.append(note_obj(None, 0, rbeat, rbeats, None, fmap, None))
        apply_beams(notes, group_beats)
        mobj = {"number": mi + 1}
        if (mi + 1) in dynamics:
            mobj["dynamics"] = [{"beat": 0.0, "text": t} for t in dynamics[mi + 1]]
        mobj["notes"] = notes
        measures.append(mobj)

    return {
        "metadata": {
            "title": args.title,
            "composer": args.composer,
            "key": detected_key,
            "tempo": args.tempo,
            "clef": args.clef,
            "timeSignature": f"{args.numerator}/{args.denominator}",
            "collectionId": args.collection,
            "difficulty": args.difficulty,
            "version": args.version,
            "isFree": args.free,
        },
        "measures": measures,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--title", required=True)
    ap.add_argument("--composer", required=True)
    ap.add_argument("--key", default="auto", help='key name, or "auto" to detect')
    ap.add_argument("--tempo", type=int, required=True)
    ap.add_argument("--clef", default="treble")
    ap.add_argument("--time-sig", default="4/4", dest="time_sig")
    ap.add_argument("--collection", default="")
    ap.add_argument("--difficulty", default="beginner")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--free", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--mode", choices=["quantize", "uniform"], default="quantize")
    ap.add_argument("--unit", default="eighth", choices=list(BEATS_OF),
                    help="base note value (median IOI maps to this)")
    ap.add_argument("--final-duration", default="half", choices=list(BEATS_OF) + [""],
                    dest="final_duration")
    ap.add_argument("--dynamics", default="")
    ap.add_argument("--no-fingering", action="store_true")
    args = ap.parse_args()
    args.numerator, args.denominator = (int(x) for x in args.time_sig.split("/"))
    unit_beats = BEATS_OF[args.unit]

    division, notes, dropped = extract_notes(args.input)
    detected_key = detect_key(notes, division) if args.key == "auto" else args.key

    if args.mode == "uniform":
        entries = [(p, m, unit_beats, None) for (_t, p, m, _d) in notes]
        base = None
    else:
        entries, base = quantize_durations(notes, division, unit_beats)

    doc = build(entries, args, detected_key)
    with open(args.output, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")

    n_trip = sum(1 for e in entries if e[3] is not None)
    print(f"{os.path.basename(args.output)}: key={detected_key} ts={args.time_sig} "
          f"notes={len(notes)} measures={len(doc['measures'])} triplets={n_trip} "
          f"dblstop_dropped={dropped} mode={args.mode}")


if __name__ == "__main__":
    main()
