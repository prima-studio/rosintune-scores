#!/usr/bin/env python3
"""Download the PUBLIC-DOMAIN Suzuki Violin Book 1 pieces (chrisspen/abc-music on
GitLab) as MusicXML and convert them into scores/violin/suzuki-book-1/ via
musicxml_to_score.py, then register them in score-index.json.

Copyrighted Suzuki-composed pieces (Allegro, Perpetual Motion, Allegretto,
Andantino, Etude A/B) are intentionally EXCLUDED.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "assets", "violin", "suzuki-src")
OUT = os.path.join(ROOT, "scores", "violin", "suzuki-book-1")
BASE = "https://gitlab.com/chrisspen/abc-music/-/raw/master/suzuki-violin-book-1"

# (source .xml, output .json, title, composer) — public domain only, book order
PIECES = [
    ("01-twinkle-twinkle-little-star.xml", "twinkle-twinkle-little-star.json", "Twinkle Twinkle Little Star", "Traditional"),
    ("02-lightly-row.xml", "lightly-row.json", "Lightly Row", "Traditional"),
    ("03-song-of-the-wind.xml", "song-of-the-wind.json", "Song of the Wind", "Traditional"),
    ("04-go-tell-aunt-rhody.xml", "go-tell-aunt-rhody.json", "Go Tell Aunt Rhody", "Traditional"),
    ("05-o-come-little-children.xml", "o-come-little-children.json", "O Come, Little Children", "J.A.P. Schulz"),
    ("06-may-song.xml", "may-song.json", "May Song", "Traditional"),
    ("07-long-long-ago.xml", "long-long-ago.json", "Long, Long Ago", "T.H. Bayly"),
    ("13-minuet-no-1.xml", "minuet-no-1.json", "Minuet No. 1", "J.S. Bach"),
    ("14-minuet-no-2.xml", "minuet-no-2.json", "Minuet No. 2", "J.S. Bach"),
    ("15-minuet-no-3.xml", "minuet-no-3.json", "Minuet No. 3", "J.S. Bach"),
    ("16-the-happy-farmer.xml", "the-happy-farmer.json", "The Happy Farmer", "R. Schumann"),
    ("17-gavotte.xml", "gavotte.json", "Gavotte", "F.J. Gossec"),
]

BEATS = {"whole": 4.0, "dotted-half": 3.0, "half": 2.0, "dotted-quarter": 1.5,
         "quarter": 1.0, "dotted-eighth": 0.75, "eighth": 0.5,
         "dotted-16th": 0.375, "16th": 0.25, "32nd": 0.125}


def download(xml):
    dst = os.path.join(SRC, xml)
    if not os.path.exists(dst):
        subprocess.run(["curl", "-sSL", "-A", "Mozilla/5.0", "-o", dst, f"{BASE}/{xml}"],
                       check=True)
    return dst


def validate(path):
    d = json.load(open(path))
    num, den = (int(x) for x in d["metadata"]["timeSignature"].split("/"))
    bar = num * (4.0 / den)
    last = d["measures"][-1]["number"]
    bad = []
    for m in d["measures"]:
        s = sum(BEATS[n["duration"]] * (n["tuplet"]["inSpaceOf"] / n["tuplet"]["count"]
                                        if n.get("tuplet") else 1) for n in m["notes"])
        # allow a short first (pickup) or last measure
        if abs(s - bar) > 1e-6 and m["number"] not in (d["measures"][0]["number"], last):
            bad.append((m["number"], round(s, 3)))
    return bad


def main():
    os.makedirs(SRC, exist_ok=True)
    entries = []
    for xml, out_name, title, composer in PIECES:
        src = download(xml)
        out = os.path.join(OUT, out_name)
        cmd = [sys.executable, os.path.join(HERE, "musicxml_to_score.py"), src, out,
               "--title", title, "--composer", composer,
               "--collection", "suzuki-book-1", "--difficulty", "beginner"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"FAIL {out_name}:\n{r.stderr}")
            continue
        bad = validate(out)
        print(r.stdout.strip() + (f"  BAD_BARS={bad}" if bad else ""))
        entries.append({
            "filename": f"violin/suzuki-book-1/{out_name}", "title": title,
            "composer": composer, "difficulty": "beginner",
            "stage": "published", "version": 1,
        })

    idx_path = os.path.join(ROOT, "score-index.json")
    idx = json.load(open(idx_path))
    for c in idx["collections"]:
        if c["id"] == "suzuki-book-1":
            c["scores"] = entries
    with open(idx_path, "w") as f:
        json.dump(idx, f, indent=2)
        f.write("\n")
    print(f"\nupdated score-index.json: suzuki-book-1 -> {len(entries)} pieces")


if __name__ == "__main__":
    main()
