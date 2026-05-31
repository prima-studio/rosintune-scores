#!/usr/bin/env python3
"""Convert a MusicXML (.mxl/.musicxml/.xml) violin part into a rosintune score JSON.

MusicXML carries exact notation (durations, meter, key, beams, ties, slurs,
fingerings, bowings, dynamics, chords) so the result is accurate - unlike the
rubato MIDI path. Use this for the Kayser études.

Example:
  python3 scripts/musicxml_to_score.py assets/violin/kayser-etudes/kayser-etude-no-1.mxl \
      scores/violin/kayser-36-studies/etude-01.json \
      --title "Etude No. 1" --composer "H.E. Kayser" --collection kayser-36-studies \
      --difficulty beginner
"""
import argparse
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from itertools import groupby

from midi_to_score import VIOLIN_FIRST_POS, MIDDLE_LINE  # reuse violin mapping

# <type> (+ dots) -> JSON duration token
TYPE_TOKEN = {
    "whole": "whole", "half": "half", "quarter": "quarter",
    "eighth": "eighth", "16th": "16th", "32nd": "32nd",
}
DOTTED = {"half": "dotted-half", "quarter": "dotted-quarter",
          "eighth": "dotted-eighth", "16th": "dotted-16th"}
TYPE_BEATS = {"whole": 4.0, "half": 2.0, "quarter": 1.0,
              "eighth": 0.5, "16th": 0.25, "32nd": 0.125}

STEP_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
MAJOR_KEYS = {-7: "Cb", -6: "Gb", -5: "Db", -4: "Ab", -3: "Eb", -2: "Bb", -1: "F",
              0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#", 7: "C#"}
MINOR_KEYS = {-7: "Ab", -6: "Eb", -5: "Bb", -4: "F", -3: "C", -2: "G", -1: "D",
              0: "A", 1: "E", 2: "B", 3: "F#", 4: "C#", 5: "G#", 6: "D#", 7: "A#"}
ALTER_SHOW = {-2: "bb", -1: "b", 0: "n", 1: "#", 2: "##"}


def load_root(path):
    if path.endswith(".mxl") or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            root_name = None
            try:  # container.xml points at the real score file
                container = ET.fromstring(z.read("META-INF/container.xml"))
                rf = container.find(".//{*}rootfile")
                if rf is not None:
                    root_name = rf.get("full-path")
            except KeyError:
                pass
            if not root_name:
                root_name = next(n for n in z.namelist()
                                 if n.endswith(".xml") and not n.startswith("META-INF"))
            data = z.read(root_name)
    else:
        data = open(path, "rb").read()
    # strip namespaces for simple tag access
    text = data.decode("utf-8", "replace")
    return ET.fromstring(text)


def midi_of(step, alter, octave):
    return 12 * (octave + 1) + STEP_SEMI[step] + alter


def pitch_name(step, alter, octave):
    acc = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}[alter]
    return f"{step}{acc}{octave}"


def key_name(fifths, mode):
    table = MINOR_KEYS if mode == "minor" else MAJOR_KEYS
    tonic = table.get(fifths, "C")
    return f"{tonic} {'Minor' if mode == 'minor' else 'Major'}"


def first(el, tag):
    return el.find(tag)


def text_of(el, tag, default=None):
    f = el.find(tag)
    return f.text if f is not None and f.text is not None else default


def convert(root, args):
    part = root.find(".//part")
    if part is None:
        sys.exit("error: no <part> found")

    # grace notes carry no metric duration and are dropped (no grace support yet)
    grace_count = sum(1 for n in part.iter("note") if n.find("grace") is not None)
    # primary voice = most common across the whole part (the line that tiles bars);
    # computed globally so it stays consistent measure to measure
    voices = Counter(text_of(n, "voice", "1") for n in part.iter("note")
                     if n.find("grace") is None)
    primary_voice = voices.most_common(1)[0][0] if voices else "1"
    voice_count = len(voices)

    divisions = 1
    fifths, mode = 0, None
    ts_num, ts_den = 4, 4
    tempo = None
    last_tonic_pc = None
    dropped_poly = 0
    bow = "up"          # first stroke flips to down-bow; carried across all measures
    in_slur = False

    measures_out = []
    for m in part.findall("measure"):
        attrs = m.find("attributes")
        if attrs is not None:
            divisions = int(text_of(attrs, "divisions", divisions))
            key = attrs.find("key")
            if key is not None:
                fifths = int(text_of(key, "fifths", fifths))
                mode = text_of(key, "mode", mode)
            time = attrs.find("time")
            if time is not None:
                ts_num = int(text_of(time, "beats", ts_num))
                ts_den = int(text_of(time, "beat-type", ts_den))
        for snd in m.iter("sound"):
            if snd.get("tempo"):
                tempo = int(round(float(snd.get("tempo"))))

        # Walk the measure assigning every note an onset (chords share the prior
        # onset; <backup>/<forward> move the cursor for other voices). Then merge
        # ALL voices by onset: at each beat the top pitch is the primary note and
        # lower pitches become `chord` entries, and a note in any voice fills a
        # rest another voice has at that beat (melody that crosses voices).
        dyn_marks = []
        events = []          # (onset_beat, note_dict)
        cursor = 0.0
        last_onset = 0.0
        for el in list(m):
            if el.tag == "direction":
                d = el.find(".//dynamics")
                if d is not None:
                    txt = next((c.tag for c in d), None)
                    if txt:
                        dyn_marks.append((round(cursor, 4), txt))
                continue
            if el.tag == "backup":
                cursor -= int(text_of(el, "duration", 0)) / divisions
                continue
            if el.tag == "forward":
                cursor += int(text_of(el, "duration", 0)) / divisions
                continue
            if el.tag != "note":
                continue
            if el.find("grace") is not None:
                continue  # grace/ornament notes carry no metric duration
            nd = parse_note(el, divisions)
            if nd["is_chord"]:
                onset = last_onset
            else:
                onset = cursor
                last_onset = cursor
                cursor += nd["beats"]
            events.append((round(onset, 4), text_of(el, "voice", "1"), nd))

        # The primary voice (most common) tiles the bar; emit one slot per primary
        # onset. Other-voice pitched notes at the same onset join as double-stops
        # or fill a primary rest. Notes whose onset isn't shared with the primary
        # voice are true polyphony the single-voice JSON can't hold -> dropped.
        by_onset = {}
        for o, v, nd in events:
            by_onset.setdefault(o, []).append((v, nd))
        primary_onsets = sorted(o for o, g in by_onset.items()
                                if any(v == primary_voice for v, _ in g))
        if not primary_onsets:  # measure has no primary-voice note -> keep all onsets
            primary_onsets = sorted(by_onset)

        notes = []
        for o in primary_onsets:
            pitched = sorted((nd for _v, nd in by_onset[o] if nd["pitch"] is not None),
                             key=lambda d: d["midi"], reverse=True)
            if pitched:
                obj = make_note(pitched[0], o, args)
                if len(pitched) > 1:
                    obj["chord"] = [chord_entry(d, args) for d in pitched[1:]]
                # bowing: honor explicit mark; else alternate once per bow-stroke
                # (a slur or tie continues the current stroke -> no flip)
                if obj["bowing"] is not None:
                    bow = obj["bowing"]
                else:
                    if not (in_slur or obj["tie"] == "end"):
                        bow = "up" if bow == "down" else "down"
                    obj["bowing"] = bow
                if obj["slur"] == "start":
                    in_slur = True
                elif obj["slur"] == "end":
                    in_slur = False
                notes.append(obj)
            else:  # primary rest with no filling note -> rest
                notes.append(make_note(max((nd for _v, nd in by_onset[o]),
                                           key=lambda d: d["beats"]), o, args))
        dropped_poly += sum(1 for o, g in by_onset.items() if o not in set(primary_onsets)
                            for v, nd in g if nd["pitch"] is not None)

        # capture tonic from last melodic note (for major/minor disambiguation)
        for nobj in reversed(notes):
            if nobj["pitch"] is not None and nobj.get("_midi") is not None:
                last_tonic_pc = nobj["_midi"] % 12
                break

        mobj = {"number": int(m.get("number"))}
        if dyn_marks:
            mobj["dynamics"] = [{"beat": round(b, 4), "text": t} for b, t in dyn_marks]
        # strip private fields
        for nobj in notes:
            nobj.pop("_midi", None)
        mobj["notes"] = notes
        measures_out.append((mobj, ts_num, ts_den))

    if grace_count or dropped_poly:
        print(f"  note: dropped {grace_count} grace + {dropped_poly} overlapping "
              f"polyphony notes ({voice_count} voice(s), merged by onset)", file=sys.stderr)

    # finalize key/mode
    if args.key != "auto":
        key_str = args.key
    else:
        resolved_mode = mode
        if resolved_mode is None:  # disambiguate relative major/minor via final tonic
            maj_pc = STEP_SEMI_OF(MAJOR_KEYS.get(fifths, "C"))
            min_pc = STEP_SEMI_OF(MINOR_KEYS.get(fifths, "A"))
            resolved_mode = "minor" if last_tonic_pc == min_pc else "major"
        key_str = key_name(fifths, resolved_mode)

    final_tempo = args.tempo if args.tempo else (tempo or 100)
    ts0 = measures_out[0][1], measures_out[0][2]

    return {
        "metadata": {
            "title": args.title,
            "composer": args.composer,
            "key": key_str,
            "tempo": final_tempo,
            "clef": args.clef,
            "timeSignature": f"{ts0[0]}/{ts0[1]}",
            "collectionId": args.collection,
            "difficulty": args.difficulty,
            "version": args.version,
            "isFree": args.free,
        },
        "measures": [mo for (mo, _n, _d) in measures_out],
    }


def STEP_SEMI_OF(tonic):
    base = STEP_SEMI[tonic[0]]
    if tonic.endswith("#"):
        base += 1
    elif tonic.endswith("b"):
        base -= 1
    return base % 12


def parse_note(el, divisions):
    is_chord = el.find("chord") is not None
    rest = el.find("rest") is not None
    dur_div = int(text_of(el, "duration", 0))
    beats = dur_div / divisions          # actual sounding beats (tuplet-reduced)
    ntype = text_of(el, "type")
    dots = len(el.findall("dot"))
    if ntype and dots and ntype in DOTTED:
        token = DOTTED[ntype]
    elif ntype in TYPE_TOKEN:
        token = TYPE_TOKEN[ntype]
    else:
        token = nearest_token(beats)
    # tuplet: duration token stays the *written* value; a tuplet field carries
    # the ratio so the renderer can recompute effective beats.
    tuplet = None
    tm = el.find("time-modification")
    if tm is not None:
        actual = int(text_of(tm, "actual-notes", 1))
        normal = int(text_of(tm, "normal-notes", 1))
        if actual != normal:
            tuplet = {"count": actual, "inSpaceOf": normal, "base": token}

    pitch = midi = None
    show = stem = None
    string = fing = bowing = artic = beam = slur = tie = None
    if not rest:
        p = el.find("pitch")
        step = text_of(p, "step")
        alter = int(text_of(p, "alter", 0))
        octave = int(text_of(p, "octave"))
        pitch = pitch_name(step, alter, octave)
        midi = midi_of(step, alter, octave)
        acc = el.find("accidental")
        if acc is not None and acc.text:
            show = {"sharp": "#", "flat": "b", "natural": "n",
                    "double-sharp": "##", "flat-flat": "bb"}.get(acc.text)
        st = text_of(el, "stem")
        stem = st if st in ("up", "down") else None
    return {
        "is_chord": is_chord, "rest": rest, "pitch": pitch, "midi": midi,
        "beats": beats, "token": token, "show": show, "stem": stem,
        "tuplet": tuplet, "el": el,
    }


def nearest_token(beats):
    cands = [(4.0, "whole"), (3.0, "dotted-half"), (2.0, "half"),
             (1.5, "dotted-quarter"), (1.0, "quarter"), (0.75, "dotted-eighth"),
             (0.5, "eighth"), (0.375, "dotted-16th"), (0.25, "16th"), (0.125, "32nd")]
    return min(cands, key=lambda bt: abs(bt[0] - beats))[1]


def make_note(d, beat, args):
    """Build a note object. `bowing` is the *explicit* mark from the score (or
    None); inferred alternating bowing is assigned by the caller per stroke."""
    el = d["el"]
    pitch, midi = d["pitch"], d["midi"]
    string = fing = bowing = artic = slur = tie = None
    if pitch is not None:
        notations = el.find("notations")
        tech = el.find(".//technical")
        if tech is not None:
            if tech.find("up-bow") is not None:
                bowing = "up"
            elif tech.find("down-bow") is not None:
                bowing = "down"
        if notations is not None:
            t = el.find("tie")
            tn = el.find(".//tied")
            tt = (t.get("type") if t is not None else
                  tn.get("type") if tn is not None else None)
            tie = {"start": "start", "stop": "end"}.get(tt)
            sl = notations.find("slur")
            if sl is not None:
                slur = {"start": "start", "stop": "end"}.get(sl.get("type"))
            arts = notations.find("articulations")
            if arts is not None:
                if arts.find("staccato") is not None:
                    artic = "."
                elif arts.find("accent") is not None:
                    artic = ">"
        string, fing = string_fingering(el, pitch, args.no_fingering)
    stem = d["stem"] or (None if pitch is None else ("down" if midi >= MIDDLE_LINE else "up"))
    obj = {
        "pitch": pitch,
        "duration": d["token"],
        "beat": round(beat, 4),
        "show": d["show"],
        "stem": stem,
        "flags": 0,
        "string": string,
        "fingering": fing,
        "bowing": (None if pitch is None else bowing),
        "articulation": artic,
        "beam": _beam(el, pitch),
        "slur": slur,
        "tie": tie,
        "_midi": midi,
    }
    if d.get("tuplet"):
        obj["tuplet"] = d["tuplet"]
    return obj


def string_fingering(el, pitch, no_fingering):
    """(string, fingering) from MusicXML <technical>, else violin first-position map."""
    string = fing = None
    tech = el.find(".//technical")
    if tech is not None:
        fnode = tech.find("fingering")
        if fnode is not None and fnode.text:
            fing = fnode.text
        snode = tech.find("string")
        if snode is not None and snode.text:
            string = int(snode.text)
    if string is None and not no_fingering:
        sf = VIOLIN_FIRST_POS.get(pitch)
        if sf:
            string = sf[0]
            if fing is None:
                fing = sf[1]
    return string, fing


def chord_entry(d, args):
    """A lower notehead of a chord: per-notehead attributes only (rhythm/connectors
    are inherited from the parent note)."""
    el = d["el"]
    string, fing = string_fingering(el, d["pitch"], args.no_fingering)
    t = el.find("tie")
    tn = el.find(".//tied")
    tt = (t.get("type") if t is not None else tn.get("type") if tn is not None else None)
    entry = {
        "pitch": d["pitch"],
        "show": d["show"],
        "string": string,
        "fingering": fing,
        "tie": {"start": "start", "stop": "end"}.get(tt),
    }
    return entry


def _beam(el, pitch):
    if pitch is None:
        return None
    b = el.find("beam")
    if b is None:
        return None
    return {"begin": "start", "end": "end"}.get(b.text)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--title", required=True)
    ap.add_argument("--composer", required=True)
    ap.add_argument("--key", default="auto", help='key, or "auto" to read from MusicXML')
    ap.add_argument("--tempo", type=int, default=0, help="override tempo (else from XML)")
    ap.add_argument("--clef", default="treble")
    ap.add_argument("--collection", default="")
    ap.add_argument("--difficulty", default="beginner")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--free", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--no-fingering", action="store_true")
    args = ap.parse_args()

    root = load_root(args.input)
    doc = convert(root, args)
    with open(args.output, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    md = doc["metadata"]
    n_notes = sum(len(m["notes"]) for m in doc["measures"])
    print(f"{os.path.basename(args.output)}: key={md['key']} ts={md['timeSignature']} "
          f"tempo={md['tempo']} measures={len(doc['measures'])} notes={n_notes}")


if __name__ == "__main__":
    main()
