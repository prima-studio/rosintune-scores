#!/usr/bin/env python3
"""Batch-convert downloaded Kayser .mxl files into scores/violin/kayser-36-studies/.

Maps assets/violin/kayser-etudes/*.mxl -> etude-NN.json by the number in the
filename, runs musicxml_to_score.py for each, then validates measure beat-sums.
"""
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "assets", "violin", "kayser-etudes")
OUT = os.path.join(ROOT, "scores", "violin", "kayser-36-studies")

BEATS = {"whole": 4.0, "dotted-half": 3.0, "half": 2.0, "dotted-quarter": 1.5,
         "quarter": 1.0, "dotted-eighth": 0.75, "eighth": 0.5,
         "dotted-16th": 0.375, "16th": 0.25, "32nd": 0.125}
# durations the current iOS build supports (before RENDERER_NOTES changes land)
IOS_SUPPORTED = {"whole", "half", "quarter", "eighth", "16th",
                 "dotted-half", "dotted-quarter"}


def difficulty(n):
    if n <= 2:
        return "beginner"
    if n <= 20:
        return "intermediate"
    return "advanced"


def eff_beats(note):
    b = BEATS[note["duration"]]
    t = note.get("tuplet")
    if t:
        b *= t["inSpaceOf"] / t["count"]
    return b


def validate(path):
    d = json.load(open(path))
    num, den = (int(x) for x in d["metadata"]["timeSignature"].split("/"))
    bar = num * (4.0 / den)
    bad = []
    tokens = set()
    needs = set()
    for m in d["measures"]:
        s = 0.0
        for n in m["notes"]:
            tokens.add(n["duration"])
            if n["duration"] not in IOS_SUPPORTED or n.get("tuplet"):
                needs.add(n["duration"] if n["duration"] not in IOS_SUPPORTED else "tuplet")
            s += eff_beats(n)
        # allow first (pickup) and last (short) bars to be short
        if abs(s - bar) > 1e-6 and m["number"] not in (1, d["measures"][-1]["number"]):
            bad.append((m["number"], round(s, 3)))
    return bad, tokens, needs


def main():
    files = sorted(glob.glob(os.path.join(SRC, "*.mxl")),
                   key=lambda p: int(re.search(r"no-?(\d+)", os.path.basename(p)).group(1)))
    rows = []
    for f in files:
        n = int(re.search(r"no-?(\d+)", os.path.basename(f)).group(1))
        out = os.path.join(OUT, f"etude-{n:02d}.json")
        cmd = [sys.executable, os.path.join(HERE, "musicxml_to_score.py"), f, out,
               "--title", f"Etude No. {n}", "--composer", "H.E. Kayser",
               "--collection", "kayser-36-studies", "--difficulty", difficulty(n)]
        cmd += ["--free"] if n == 1 else ["--no-free"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"#{n:02d} FAILED:\n{r.stderr}")
            continue
        bad, tokens, needs = validate(out)
        rows.append((n, bad, needs))
        flag = f"  BAD_BARS={bad}" if bad else ""
        need = f"  needs_ios={sorted(needs)}" if needs else ""
        print(r.stdout.strip() + need + flag)

    update_index([n for n, _b, _x in rows])

    # summary of iOS-side requirements across the set
    all_needs = set()
    for _n, _bad, needs in rows:
        all_needs |= needs
    print("\n=== iOS support needed by etudes 1-20:", sorted(all_needs))
    badcount = sum(1 for _n, bad, _ in rows if bad)
    print(f"=== {len(rows)} converted, {badcount} with non-trivial bad bars")


def update_index(numbers):
    """Rewrite the kayser collection's score list in score-index.json."""
    path = os.path.join(ROOT, "score-index.json")
    idx = json.load(open(path))
    for coll in idx["collections"]:
        if coll["id"] != "kayser-36-studies":
            continue
        coll["scores"] = [{
            "filename": f"violin/kayser-36-studies/etude-{n:02d}.json",
            "title": f"Etude No. {n}",
            "composer": "H.E. Kayser",
            "difficulty": difficulty(n),
            "stage": "staging",
            "version": 1,
        } for n in sorted(numbers)]
    with open(path, "w") as f:
        json.dump(idx, f, indent=2)
        f.write("\n")
    print(f"updated score-index.json: kayser collection -> {len(numbers)} études")


if __name__ == "__main__":
    main()
