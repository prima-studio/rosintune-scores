#!/usr/bin/env python3
"""Unified score pipeline.

Converts every source under assets/ into a rosintune score JSON, rebuilds
score-index.json, and regenerates the inventory CSVs (products.csv for the App
Store + scores-inventory.csv as a human-readable master catalog).

Conversion is automatic: a source's collection / title / composer / difficulty
are derived from the folder it lives in (see FOLDERS). Likely-copyrighted files
and duplicate versions of the same numbered etude are skipped (logged, never
silently). The run is idempotent and re-runnable: existing per-piece curation in
score-index.json (stage, price, isFree, version, and any hand-edited title /
composer / difficulty) is PRESERVED on merge -- only brand-new pieces get the
folder defaults, and collections with no matching asset folder are left intact.

  python3 scripts/build_scores.py                       # build everything
  python3 scripts/build_scores.py --only kreutzer-42-studies first-folk-tunes
  python3 scripts/build_scores.py --dry-run             # show plan, convert nothing
  python3 scripts/build_scores.py --prune               # also delete orphaned JSON

Drop new sources into assets/ and re-run: files added to a known folder are
picked up automatically; an entirely NEW folder is auto-discovered and added as
its own collection (conservative defaults, flagged for review). A full run also
reports any source it couldn't place (coverage) and any generated JSON whose
source disappeared (orphans -> --prune to delete).
"""
import argparse
import csv
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
SCORES = os.path.join(ROOT, "scores")
INDEX = os.path.join(ROOT, "score-index.json")
CONVERTER = os.path.join(HERE, "musicxml_to_score.py")
GEN_PRODUCTS = os.path.join(HERE, "generate-app-store-products.py")
INVENTORY_CSV = os.path.join(ROOT, "scores-inventory.csv")

EXTS = (".mxl", ".musicxml", ".xml")

# beats per duration token, for bar-sum validation
BEATS = {"whole": 4.0, "dotted-half": 3.0, "half": 2.0, "dotted-quarter": 1.5,
         "quarter": 1.0, "dotted-eighth": 0.75, "eighth": 0.5,
         "dotted-16th": 0.375, "16th": 0.25, "32nd": 0.125}
# durations/features the shipping iOS build handles without RENDERER_NOTES changes
IOS_SUPPORTED = {"whole", "half", "quarter", "eighth", "16th",
                 "dotted-half", "dotted-quarter"}
# A piece whose share of double-stop (chord) notes exceeds this is dropped: too
# chord-heavy for the single-line reader. Computed over sounding (pitched) notes.
MAX_DOUBLE_STOP = 0.10

# Sources to exclude entirely (copyright). Matched as substrings of the path.
SKIP_SUBSTR = [
    "por-una-cabeza",   # Por una Cabeza - Carlos Gardel (still in copyright)
    "xiao-ti-qin",      # 王振山 arranger's edition of the Kayser set (engraving (c))
]
# Filename keywords that demote a candidate when several map to the same number
# (we keep the cleanest violin source for each numbered etude/caprice).
DEMERIT = {"arr-for": 6, "solo-flute": 6, "flute": 5, "oboe": 4, "clarinet": 4,
           "slow-practice": 3, "various-bowings": 2, "practice": 1,
           "tmea": 1, "all-state": 1}


# ---- per-collection irregular tables (source filename -> metadata) ----------
# Beginner folk tunes & short classics (all public-domain compositions):
# per-piece composers differ, so they can't be folder-derived. Dict order =
# collection order.
FIRST_PIECES = {
    "01-twinkle-twinkle-little-star.xml": ("twinkle-twinkle-little-star.json", "Twinkle Twinkle Little Star", "Traditional", None),
    "02-lightly-row.xml": ("lightly-row.json", "Lightly Row", "Traditional", None),
    "04-go-tell-aunt-rhody.xml": ("go-tell-aunt-rhody.json", "Go Tell Aunt Rhody", "Traditional", None),
    "06-may-song.xml": ("may-song.json", "May Song", "Traditional", None),
    "03-song-of-the-wind.xml": ("song-of-the-wind.json", "Song of the Wind", "Traditional", None),
    "07-long-long-ago.xml": ("long-long-ago.json", "Long, Long Ago", "T.H. Bayly", None),
    "05-o-come-little-children.xml": ("o-come-little-children.json", "O Come, Little Children", "J.A.P. Schulz", None),
    "16-the-happy-farmer.xml": ("the-happy-farmer.json", "The Happy Farmer", "R. Schumann", None),
    "13-minuet-no-1.xml": ("minuet-no-1.json", "Minuet I", "J.S. Bach", None),
    "14-minuet-no-2.xml": ("minuet-no-2.json", "Minuet II", "J.S. Bach", None),
    "15-minuet-no-3.xml": ("minuet-no-3.json", "Minuet III", "J.S. Bach", None),
    "17-gavotte.xml": ("gavotte.json", "Gavotte", "F.J. Gossec", None),
}
# Demo collection: (out, title, composer, staff). Ode to Joy is a piano source
# whose melody is staff 1.
DEMO = {
    "lightly-row.musicxml": ("lightly-row.json", "Lightly Row", "Traditional", None),
    "ode-to-joy.mxl": ("ode-to-joy.json", "Ode to Joy", "L. van Beethoven", 1),
}


def kayser_difficulty(n):
    return "beginner" if n <= 2 else ("intermediate" if n <= 20 else "advanced")


# Each entry describes how to turn an assets/ subfolder into a collection.
#   scheme "number" -> numbered series: dedup by number, title "<noun> No. N",
#                      out "<slug>-NN.json".
#   scheme "named"  -> one piece per file: title/slug derived from the filename.
#   scheme "table"  -> only the listed files, with explicit metadata.
FOLDERS = [
    dict(folder="violin/demo-src", collection="example", coll_title="Example",
         subtitle=None, coll_composer=None, is_free=True, sort=0,
         out_dir="violin/example", scheme="table", table=DEMO,
         difficulty=lambda key: "beginner", price=0.0, stage="published"),
    dict(folder="violin/first-pieces", collection="first-folk-tunes",
         coll_title="First Folk Tunes & Classics", subtitle=None, coll_composer=None,
         is_free=True, sort=1, out_dir="violin/first-folk-tunes", scheme="table",
         table=FIRST_PIECES, difficulty=lambda key: "beginner", price=0.0,
         stage="published", mechanical_editorial=True),
    dict(folder="violin/kayser-etudes", collection="kayser-36-studies",
         coll_title="Kayser 36 Studies",
         subtitle="Elementary and Progressive Studies, Op. 20",
         coll_composer="H.E. Kayser", is_free=False, sort=2,
         out_dir="violin/kayser-36-studies", scheme="number", noun="Etude",
         slug="etude", composer="H.E. Kayser", difficulty=kayser_difficulty,
         free_numbers={1}, price=0.99, stage="staging"),
    dict(folder="violin/kreutzer", collection="kreutzer-42-studies",
         coll_title="Kreutzer 42 Studies", subtitle="42 Études ou Caprices",
         coll_composer="R. Kreutzer", is_free=False, sort=3,
         out_dir="violin/kreutzer-42-studies", scheme="number", noun="Etude",
         slug="etude", composer="R. Kreutzer", difficulty=lambda n: "advanced",
         free_numbers=set(), price=0.99, stage="staging"),
    dict(folder="violin/pierre-rode", collection="rode-24-caprices",
         coll_title="Rode 24 Caprices", subtitle="Op. 22",
         coll_composer="Pierre Rode", is_free=False, sort=4,
         out_dir="violin/rode-24-caprices", scheme="number", noun="Caprice",
         slug="caprice", composer="Pierre Rode", difficulty=lambda n: "advanced",
         free_numbers=set(), price=0.99, stage="staging"),
    dict(folder="violin/bach", collection="bach-for-violin",
         coll_title="Bach for Violin", subtitle=None, coll_composer="J.S. Bach",
         is_free=False, sort=5, out_dir="violin/bach-for-violin", scheme="named",
         composer="J.S. Bach", difficulty=lambda key: "advanced", price=0.99,
         stage="staging"),
    # The 6 Sonatas & Partitas for solo violin: each source is a multi-movement
    # work, split into one JSON per movement (each movement has a single meter).
    dict(folder="violin/bach-sonatas-partitas", collection="bach-sonatas-partitas",
         coll_title="Bach Sonatas & Partitas", subtitle="Sei Solo, BWV 1001–1006",
         coll_composer="J.S. Bach", is_free=False, sort=6,
         out_dir="violin/bach-sonatas-partitas", scheme="movements",
         composer="J.S. Bach", difficulty=lambda key: "advanced", price=0.99,
         stage="staging"),
]


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def music_files(folder_abs):
    out = []
    for p in sorted(glob.glob(os.path.join(folder_abs, "*"))):
        if p.lower().endswith(EXTS):
            out.append(p)
    return out


def is_skipped(path):
    low = path.lower()
    return any(s in low for s in SKIP_SUBSTR)


def extract_number(stem):
    """Pull the etude/caprice number out of a messy filename, ignoring opus,
    BWV, year and collection-size numbers (e.g. '42-studies', '24-caprices')."""
    s = stem.lower()
    s = re.sub(r"op[-.\s]*\d+", " ", s)
    s = re.sub(r"bwv[-.\s]*\d+", " ", s)
    s = re.sub(r"(19|20)\d\d(-\d+)?", " ", s)
    s = re.sub(r"\d+-(studies|studien|caprices|etudes|études)", " ", s)
    m = re.search(r"(?:^|[^a-z0-9])no\.?\s*-?\s*(\d+)", s)        # no. N / no-N / noN
    if m:
        return int(m.group(1))
    m = re.search(r"(etude|caprice|study|étude)\D{0,3}(\d+)", s)
    if m:
        return int(m.group(2))
    m = re.search(r"^\s*(\d+)\b", s)                              # leading number
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def has_violin_part(path):
    """True if the source advertises a violin part (used to demote arrangements
    for other instruments). Mislabeled single-part files still convert fine, so
    this is only a tie-breaker, never a hard filter."""
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        if path.lower().endswith(".mxl"):
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist()
                            if n.endswith(".xml") and not n.startswith("META-INF"))
                data = z.read(name)
        else:
            data = open(path, "rb").read()
        root = ET.fromstring(data.decode("utf-8", "replace"))
        names = [(sp.find("part-name").text or "")
                 for sp in root.findall(".//part-list/score-part")
                 if sp.find("part-name") is not None]
        from musicxml_to_score import VIOLIN_RE
        return any(VIOLIN_RE.search(n) for n in names)
    except Exception:
        return True


def demerit_score(path):
    low = os.path.basename(path).lower()
    score = sum(w for kw, w in DEMERIT.items() if kw in low)
    if not has_violin_part(path):
        score += 8
    return score


def best_of(paths):
    """Choose the cleanest source among duplicates of one piece: fewest
    demerits, then shortest filename, then alphabetical (deterministic)."""
    return min(paths, key=lambda p: (demerit_score(p), len(os.path.basename(p)),
                                     os.path.basename(p)))


def clean_title(stem):
    """Best-effort readable title from a filename (named scheme). Titles are
    editable in the index and preserved across re-runs, so this only needs to be
    a decent first pass."""
    s = stem.lower()
    junk = ["johann-sebastian-bach", "j-s-bach", "jacques-pierre-rode",
            "pierre-rode", "heinrich-ernst-kayse", "r-kreutzer",
            "for-the-violin", "for-violin", "arr-for-solo-flute",
            "with-various-bowings", "slow-practice", "all-state-2021",
            "all-state", "tmea", "-bach", "-rode", "-kreutzer", "-kayser"]
    for j in junk:
        s = s.replace(j, " ")
    s = re.sub(r"(19|20)\d\d(-\d+)?", " ", s)
    s = re.sub(r"\bbwv[-.\s]*(\d+)", r" bwv \1", s)
    s = re.sub(r"\bop[-.\s]*(\d+)", r" op \1", s)
    s = re.sub(r"\bno[-.\s]*(\d+)", r" no \1", s)
    s = re.sub(r"\b(\d+)(st|nd|rd|th)-mvt", r" \1\2 mvt", s)
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    out = []
    words = s.split(" ")
    for i, w in enumerate(words):
        if not w:
            continue
        if w == "bwv":
            out.append("BWV")
        elif w == "op" and i + 1 < len(words):
            out.append("Op.")
        elif w == "no" and i + 1 < len(words):
            out.append("No.")
        elif w == "mvt":
            out.append("Mvt")
        else:
            out.append(w[:1].upper() + w[1:])
    if out and out[0].lower() == "bach":          # composer is already in metadata
        out = out[1:]
    n = len(out)                                   # collapse "X Y X Y" -> "X Y"
    if n >= 2 and n % 2 == 0 and out[:n // 2] == out[n // 2:]:
        out = out[:n // 2]
    if len(out) >= 6:                              # filename repeats the title:
        for j in range(3, len(out)):              # cut at the 2nd occurrence of
            if out[j] == out[0]:                  # the first word
                out = out[:j]
                break
    # "a"/"an" omitted: 'A' is a key name (A Minor), not an article, in this set
    stop = {"in", "on", "the", "for", "from", "of", "and", "to"}
    out = [w if i == 0 or w.lower() not in stop else w.lower()
           for i, w in enumerate(out)]
    return " ".join(out) or stem


def slugify(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") + ".json"


def eff_beats(note):
    b = BEATS[note["duration"]]
    t = note.get("tuplet")
    if t:
        b *= t["inSpaceOf"] / t["count"]
    return b


def validate(path):
    """Return (bad_bars, ios_needs, nbars, ds_ratio). bad_bars are measures whose
    beats don't match the (single, global) time signature -- a strong signal for
    multi-movement works that change meter, which this one-TS JSON can't model.
    ds_ratio is the fraction of sounding (pitched) notes that are double stops
    (carry a `chord`), used to drop chord-heavy pieces."""
    d = json.load(open(path))
    num, den = (int(x) for x in d["metadata"]["timeSignature"].split("/"))
    bar = num * (4.0 / den)
    nbars = len(d["measures"])
    last = d["measures"][-1]["number"] if d["measures"] else 0
    first = d["measures"][0]["number"] if d["measures"] else 0
    bad, needs = [], set()
    pitched = double_stops = 0
    for m in d["measures"]:
        s = 0.0
        for n in m["notes"]:
            if n["duration"] not in IOS_SUPPORTED:
                needs.add(n["duration"])
            if n.get("tuplet"):
                needs.add("tuplet")
            if n.get("chord"):
                needs.add("chord")
            if n.get("pitch") is not None:
                pitched += 1
                if n.get("chord"):
                    double_stops += 1
            s += eff_beats(n)
        if abs(s - bar) > 1e-6 and m["number"] not in (first, last):
            bad.append((m["number"], round(s, 3)))
    ds_ratio = (double_stops / pitched) if pitched else 0.0
    return bad, needs, nbars, ds_ratio


# --------------------------------------------------------------------------- #
#  discovery: pick up NEW folders dropped into assets/ without config edits
# --------------------------------------------------------------------------- #
def all_assets_music():
    """Every music source anywhere under assets/ (recursive)."""
    out = []
    for p in glob.glob(os.path.join(ASSETS, "**", "*"), recursive=True):
        if os.path.isfile(p) and p.lower().endswith(EXTS):
            out.append(p)
    return sorted(out)


def read_xml_composer(path):
    """Best-effort composer from a source's MusicXML <creator type='composer'>.
    Returns a clean name or None (junk like '2023-2024 TMEA Etude#2' is rejected
    so auto-discovered folders don't inherit garbage)."""
    try:
        from musicxml_to_score import load_root
        root = load_root(path)
        for c in root.findall(".//identification/creator"):
            if c.get("type") == "composer" and c.text:
                t = " ".join(c.text.split())
                if t and len(t) <= 40 and not re.search(r"\d", t) \
                        and not re.search(r"(?i)tmea|edited|arr\b|all.state", t):
                    return t
    except Exception:
        pass
    return None


def common_xml_composer(files):
    c = Counter(x for x in (read_xml_composer(f) for f in files) if x)
    return c.most_common(1)[0][0] if c else None


def read_work_title(path):
    """The <work-title>/<movement-title> from a source (e.g. 'Sonata No. 1 in G
    Minor'), used to name a multi-movement work whose filename is unreliable."""
    try:
        from musicxml_to_score import load_root
        root = load_root(path)
        for tag in ("work-title", "movement-title"):
            e = root.find(f".//{tag}")
            if e is not None and e.text and e.text.strip():
                return e.text.strip()
    except Exception:
        pass
    return None


def key_from_filename(stem):
    """Home key encoded in a filename ('...-in-g-minor-...' -> 'G Minor'). These
    Baroque scores use one-flat-short signatures that defeat auto key detection,
    so we pass the work key explicitly."""
    m = re.search(r"in-([a-g])(?:-(sharp|flat))?-(minor|major)", stem.lower())
    if not m:
        return None
    note = m.group(1).upper()
    note += {"sharp": "#", "flat": "b"}.get(m.group(2), "")
    return f"{note} {m.group(3).capitalize()}"


def auto_folder_cfg(folder_rel, files):
    """Build a conservative collection config for an unconfigured asset folder so
    a re-run picks it up. Metadata is folder/XML-derived and flagged for review;
    promote it into FOLDERS for clean titles/pricing."""
    base = folder_rel.split("/")[-1]
    title = " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-_\s]+", base) if w)
    return dict(folder=folder_rel, collection=re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-"),
                coll_title=title or base, subtitle=None,
                coll_composer=common_xml_composer(files) or "Unknown",
                is_free=False, sort=90, out_dir=folder_rel, scheme="named",
                composer=common_xml_composer(files) or "Unknown",
                difficulty=lambda key: "intermediate", price=0.99,
                stage="staging", auto=True)


def discover_auto_folders():
    """Folders under assets/ that hold music but aren't in FOLDERS."""
    configured = {c["folder"] for c in FOLDERS}
    by_dir = {}
    for p in all_assets_music():
        rel = os.path.relpath(os.path.dirname(p), ASSETS).replace(os.sep, "/")
        if rel in configured:
            continue
        by_dir.setdefault(rel, []).append(p)
    return [auto_folder_cfg(rel, files) for rel, files in sorted(by_dir.items())]


def find_orphans(index, cfgs):
    """JSON files in a processed collection's output dir that no longer have an
    index entry (e.g. a source was renamed/removed). Returns list of abs paths."""
    by_id = {c["id"]: c for c in index["collections"]}
    orphans = []
    for cfg in cfgs:
        coll = by_id.get(cfg["collection"])
        kept = {s["filename"] for s in coll.get("scores", [])} if coll else set()
        out_abs = os.path.join(SCORES, cfg["out_dir"])
        for jp in glob.glob(os.path.join(out_abs, "*.json")):
            rel = os.path.relpath(jp, SCORES).replace(os.sep, "/")
            if rel not in kept:
                orphans.append(jp)
    return orphans


# --------------------------------------------------------------------------- #
#  planning: decide every (source -> output) before converting
# --------------------------------------------------------------------------- #
def plan_folder(cfg):
    """Return (pieces, skipped). Each piece is a dict the runner can convert."""
    folder_abs = os.path.join(ASSETS, cfg["folder"])
    files = music_files(folder_abs)
    skipped = [(f, "copyright") for f in files if is_skipped(f)]
    files = [f for f in files if not is_skipped(f)]
    pieces = []

    if cfg["scheme"] == "table":
        present = {os.path.basename(f): f for f in files}
        for src_name, (out_name, title, composer, staff) in cfg["table"].items():
            src = present.get(src_name)
            if src is None:
                skipped.append((os.path.join(folder_abs, src_name), "missing source"))
                continue
            pieces.append(dict(
                src=src, out=os.path.join(cfg["out_dir"], out_name),
                title=title, composer=composer, staff=staff,
                difficulty=cfg["difficulty"](title), price=cfg["price"],
                stage=cfg["stage"], free=(cfg["price"] == 0),
                mechanical=cfg.get("mechanical_editorial", False)))

    elif cfg["scheme"] == "number":
        groups = {}
        for f in files:
            n = extract_number(os.path.basename(os.path.splitext(f)[0]))
            if n is None:
                skipped.append((f, "no number in filename"))
                continue
            groups.setdefault(n, []).append(f)
        for n in sorted(groups):
            chosen = best_of(groups[n])
            for dup in groups[n]:
                if dup != chosen:
                    skipped.append((dup, f"duplicate of No.{n}"))
            free = n in cfg.get("free_numbers", set())
            pieces.append(dict(
                src=chosen,
                out=os.path.join(cfg["out_dir"], f"{cfg['slug']}-{n:02d}.json"),
                title=f"{cfg['noun']} No. {n}", composer=cfg["composer"],
                staff=None, difficulty=cfg["difficulty"](n),
                price=0.0 if free else cfg["price"],
                stage="published" if free else cfg["stage"], free=free,
                number=n))

    elif cfg["scheme"] == "named":
        seen = {}
        for f in sorted(files):
            title = clean_title(os.path.basename(os.path.splitext(f)[0]))
            out_name = slugify(title)
            if out_name in seen:
                skipped.append((f, f"duplicate slug of {seen[out_name]}"))
                continue
            seen[out_name] = os.path.basename(f)
            composer = cfg["composer"]
            if cfg.get("auto"):                       # auto folder: try the source
                composer = read_xml_composer(f) or cfg["composer"]
            pieces.append(dict(
                src=f, out=os.path.join(cfg["out_dir"], out_name), title=title,
                composer=composer, staff=None,
                difficulty=cfg["difficulty"](title), price=cfg["price"],
                stage=cfg["stage"], free=(cfg["price"] == 0)))

    elif cfg["scheme"] == "movements":
        for f in sorted(files):
            stem = os.path.basename(os.path.splitext(f)[0])
            mb = re.search(r"bwv[-\s]*(\d+)", stem.lower())
            prefix = (f"bwv{mb.group(1)}" if mb
                      else re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-"))
            work_title = read_work_title(f) or clean_title(stem)
            pieces.append(dict(
                src=f, split=True, slug_prefix=prefix, work_title=work_title,
                key=key_from_filename(stem), out_dir=cfg["out_dir"],
                composer=cfg["composer"], difficulty=cfg["difficulty"](work_title),
                price=cfg["price"], stage=cfg["stage"], free=(cfg["price"] == 0)))

    return pieces, skipped


# --------------------------------------------------------------------------- #
#  conversion + index merge
# --------------------------------------------------------------------------- #
def load_index():
    with open(INDEX) as f:
        return json.load(f)


def existing_entries(index):
    """filename -> existing score entry (so curation survives re-runs)."""
    out = {}
    for c in index.get("collections", []):
        for s in c.get("scores", []):
            out[s["filename"]] = s
    return out


def too_many_double_stops(ratio, out, prior):
    """Decide whether a converted piece is too chord-heavy to add. Deletes its
    JSON and returns a skip reason string, or None to keep it. Loudly notes when
    the dropped piece was previously published so the removal isn't silent."""
    if ratio <= MAX_DOUBLE_STOP:
        return None
    out_abs = os.path.join(SCORES, out)
    if os.path.exists(out_abs):
        os.remove(out_abs)
    note = ""
    if prior and prior.get("stage") == "published":
        note = "  ⚠ was PUBLISHED — now dropped"
    return f"double-stops {ratio * 100:.0f}% > {MAX_DOUBLE_STOP * 100:.0f}%{note}"


def convert_piece(piece, prior):
    """Run the converter, honoring any curated title/composer/difficulty/free
    from a pre-existing index entry. Returns a tagged result:
    ("ok", (entry, bad, needs, multimeter)) | ("skip", (out, reason)) |
    ("fail", None)."""
    title = prior.get("title", piece["title"]) if prior else piece["title"]
    composer = prior.get("composer", piece["composer"]) if prior else piece["composer"]
    difficulty = prior.get("difficulty", piece["difficulty"]) if prior else piece["difficulty"]
    free = piece["free"]
    out_abs = os.path.join(SCORES, piece["out"])
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    cmd = [sys.executable, CONVERTER, piece["src"], out_abs,
           "--title", title, "--composer", composer or "Unknown",
           "--collection", piece["collection"], "--difficulty", difficulty]
    cmd += ["--free"] if free else ["--no-free"]
    if piece.get("staff") is not None:
        cmd += ["--staff", str(piece["staff"])]
    if piece.get("mechanical"):
        cmd += ["--mechanical-editorial"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAIL {piece['out']}:\n{indent(r.stderr)}")
        return ("fail", None)
    bad, needs, nbars, ds_ratio = validate(out_abs)
    reason = too_many_double_stops(ds_ratio, piece["out"], prior)
    if reason:
        return ("skip", (piece["out"], reason))
    multimeter = bool(bad) and len(bad) > max(4, 0.2 * nbars)
    # build the index entry: curated fields win, defaults fill the rest
    entry = {
        "filename": piece["out"], "title": title, "composer": composer,
        "difficulty": difficulty,
        "stage": prior.get("stage", piece["stage"]) if prior else piece["stage"],
        "price": prior.get("price", piece["price"]) if prior else piece["price"],
        "version": prior.get("version", 1) if prior else 1,
    }
    if (prior.get("isFree") if prior else free):
        entry["isFree"] = True
    msg = r.stdout.strip().splitlines()[0] if r.stdout.strip() else piece["out"]
    if multimeter:
        bad_note = f"  ⚠ multi-meter? {len(bad)}/{nbars} bars off single-TS (review)"
    elif bad:
        shown = ", ".join(f"m{n}={b}" for n, b in bad[:6])
        bad_note = f"  bad_bars=[{shown}{', …' if len(bad) > 6 else ''}]"
    else:
        bad_note = ""
    need_note = f"  needs_ios={sorted(needs)}" if needs else ""
    print(f"  {msg}{bad_note}{need_note}")
    return ("ok", (entry, bad, needs, multimeter))


def convert_split_piece(piece, prior):
    """Split a multi-movement source into one JSON per movement (movements scheme)
    and return a result tuple per movement. Per-movement curation in the index
    (keyed by the movement's output filename) is preserved across re-runs."""
    out_abs_dir = os.path.join(SCORES, piece["out_dir"])
    os.makedirs(out_abs_dir, exist_ok=True)
    cmd = [sys.executable, CONVERTER, piece["src"], out_abs_dir, "--split",
           "--slug-prefix", piece["slug_prefix"], "--title", piece["work_title"],
           "--composer", piece["composer"] or "Unknown",
           "--collection", piece["collection"], "--difficulty", piece["difficulty"]]
    if piece.get("key"):
        cmd += ["--key", piece["key"]]
    cmd += ["--free"] if piece["free"] else ["--no-free"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAIL {piece['slug_prefix']}:\n{indent(r.stderr)}")
        return []
    lines = r.stdout.strip().splitlines()
    try:
        manifest = json.loads(lines[-1]) if lines else []
    except (ValueError, IndexError) as e:
        print(f"  FAIL manifest {piece['slug_prefix']}: {e}")
        return []
    print(f"  {piece['work_title']} -> {len(manifest)} movements")
    results = []
    for mv in manifest:
        relout = os.path.join(piece["out_dir"], mv["file"])
        p = prior.get(relout)
        bad, needs, nbars, ds_ratio = validate(os.path.join(SCORES, relout))
        reason = too_many_double_stops(ds_ratio, relout, p)
        if reason:
            print(f"    skip {mv['file']}  [{reason}]")
            results.append(("skip", (relout, reason)))
            continue
        multimeter = bool(bad) and len(bad) > max(4, 0.2 * nbars)
        entry = {
            "filename": relout,
            "title": p.get("title", mv["title"]) if p else mv["title"],
            "composer": p.get("composer", piece["composer"]) if p else piece["composer"],
            "difficulty": p.get("difficulty", piece["difficulty"]) if p else piece["difficulty"],
            "stage": p.get("stage", piece["stage"]) if p else piece["stage"],
            "price": p.get("price", piece["price"]) if p else piece["price"],
            "version": p.get("version", 1) if p else 1,
        }
        if (p.get("isFree") if p else piece["free"]):
            entry["isFree"] = True
        bn = ""
        if bad:
            shown = ", ".join(f"m{n}={b}" for n, b in bad[:4])
            bn = f"  bad_bars=[{shown}{', …' if len(bad) > 4 else ''}]"
        nn = f"  needs_ios={sorted(needs)}" if needs else ""
        print(f"    {mv['file']}: {mv['key']} {mv['timeSignature']} "
              f"bars={mv['measures']} notes={mv['notes']}{bn}{nn}")
        results.append(("ok", (entry, bad, needs, multimeter)))
    return results


def stamp_hashes(index):
    """Stamp each entry's `sha256` with the hash of its generated JSON. The app
    verifies downloads against these and discards mismatches, so they must be
    re-stamped on every build (a stale hash makes clients reject the file).
    Returns (stamped, missing) counts; entries whose file is missing keep no
    hash (the app treats an absent hash as 'no verification')."""
    stamped, missing = 0, []
    for col in index.get("collections", []):
        for s in col.get("scores", []):
            path = os.path.join(SCORES, s["filename"])
            if os.path.exists(path):
                with open(path, "rb") as f:
                    s["sha256"] = hashlib.sha256(f.read()).hexdigest()
                stamped += 1
            else:
                s.pop("sha256", None)
                missing.append(s["filename"])
    return stamped, missing


def indent(text, pad="    "):
    return "\n".join(pad + ln for ln in text.splitlines())


def merge_collection(index, cfg, entries):
    """Insert/update the collection in the index, keeping fields the pipeline
    doesn't own (title/subtitle/composer/isFree/sortOrder) when it already
    exists. Curated entries whose source isn't in this folder (e.g. a demo
    sourced elsewhere) are retained as long as their JSON still exists on disk,
    so a re-run never silently drops them."""
    generated = {e["filename"] for e in entries}
    for c in index["collections"]:
        if c["id"] == cfg["collection"]:
            survivors = [s for s in c.get("scores", [])
                         if s["filename"] not in generated
                         and os.path.exists(os.path.join(SCORES, s["filename"]))]
            for s in survivors:
                print(f"  keep (curated, not in folder): {s['filename']}")
            new_scores = entries + survivors
            if new_scores:               # never wipe an existing collection empty
                c["scores"] = new_scores
            return
    if not entries:                      # don't create a brand-new empty collection
        return                           # (e.g. a folder whose files were all skipped)
    index["collections"].append({
        "id": cfg["collection"], "title": cfg["coll_title"],
        "subtitle": cfg.get("subtitle"), "composer": cfg.get("coll_composer"),
        "isFree": cfg["is_free"], "sortOrder": cfg["sort"], "scores": entries,
    })


# --------------------------------------------------------------------------- #
#  CSV outputs
# --------------------------------------------------------------------------- #
def write_inventory_csv(index):
    rows = []
    for c in sorted(index["collections"], key=lambda c: c.get("sortOrder", 0)):
        for s in c.get("scores", []):
            price = s.get("price")
            is_free = s.get("isFree", c.get("isFree", False)) or price == 0
            rows.append({
                "Collection": c["id"], "Collection Title": c.get("title", ""),
                "Filename": s["filename"], "Title": s.get("title", ""),
                "Composer": s.get("composer", ""),
                "Difficulty": s.get("difficulty", ""),
                "Stage": s.get("stage", ""),
                "Price": "" if price is None else price,
                "Free": "true" if is_free else "false",
            })
    with open(INVENTORY_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Collection", "Collection Title",
                                          "Filename", "Title", "Composer",
                                          "Difficulty", "Stage", "Price", "Free"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def write_products_csv():
    out = os.path.join(ROOT, "products.csv")
    r = subprocess.run([sys.executable, GEN_PRODUCTS], capture_output=True, text=True)
    with open(out, "w") as f:
        f.write(r.stdout)
    # generator prints its count to stderr
    note = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
    return out, note


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", default=None,
                    help="only build these collection ids")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan (sources -> outputs, skips) and stop")
    ap.add_argument("--prune", action="store_true",
                    help="delete generated JSON in built dirs that no longer has "
                         "an index entry (renamed/removed sources)")
    args = ap.parse_args()

    index = load_index()
    prior = existing_entries(index)

    # configured folders + any NEW folder dropped into assets/ (auto-discovered),
    # so a re-run picks up additions with zero config edits.
    auto = discover_auto_folders()
    if auto:
        print("found unconfigured folders under assets/ "
              "(auto-added as their own collection if they hold usable sources):")
        for c in auto:
            print(f"  {c['folder']} -> collection '{c['collection']}' "
                  f"(composer={c['coll_composer']!r})")
    configs = FOLDERS + auto
    selected = [c for c in configs if not args.only or c["collection"] in args.only]
    if not selected:
        sys.exit(f"no collections match --only {args.only}")

    all_skips, all_needs, review, auto_added = [], set(), [], []
    ds_skips = []                           # dropped post-conversion: too chord-heavy
    claimed = set()                         # every source we converted or skipped
    total_pieces = total_bad = 0

    for cfg in selected:
        pieces, skipped = plan_folder(cfg)
        all_skips += skipped
        claimed |= {p["src"] for p in pieces} | {f for f, _why in skipped}
        tag = " [AUTO]" if cfg.get("auto") else ""
        print(f"\n=== {cfg['collection']}  ({cfg['folder']}){tag} "
              f"-> {len(pieces)} pieces, {len(skipped)} skipped ===")
        for f, why in skipped:
            print(f"  skip {os.path.relpath(f, ASSETS)}  [{why}]")
        if args.dry_run:
            for p in pieces:
                t = "FREE" if p["free"] else f"${p['price']}"
                if p.get("split"):
                    print(f"  plan {os.path.relpath(p['src'], ASSETS)}  ->  "
                          f"{p['out_dir']}/{p['slug_prefix']}-NN-*.json  "
                          f"(split: {p['work_title']}, {p.get('key')}, "
                          f"{p['difficulty']}, {t}, {p['stage']})")
                else:
                    print(f"  plan {os.path.relpath(p['src'], ASSETS)}  ->  "
                          f"{p['out']}  ({p['title']}, {p['difficulty']}, {t}, {p['stage']})")
            continue

        entries = []
        for p in pieces:
            p["collection"] = cfg["collection"]
            if p.get("split"):
                ress = convert_split_piece(p, prior)
            else:
                ress = [convert_piece(p, prior.get(p["out"]))]
            for kind, data in ress:
                if kind == "ok":
                    entry, bad, needs, multimeter = data
                    entries.append(entry)
                    total_pieces += 1
                    total_bad += 1 if bad else 0
                    all_needs |= needs
                    if multimeter:
                        review.append(entry["filename"])
                    if cfg.get("auto"):
                        auto_added.append(entry["filename"])
                elif kind == "skip":
                    out, reason = data
                    ds_skips.append((out, reason))
                    if not p.get("split"):           # split prints its own skip line
                        print(f"  skip {out}  [{reason}]")
        merge_collection(index, cfg, entries)

    if args.dry_run:
        print("\n(dry run -- no files written)")
        return

    n_hashed, hash_missing = stamp_hashes(index)

    with open(INDEX, "w") as f:
        json.dump(index, f, indent=2)
        f.write("\n")

    inv_n = write_inventory_csv(index)
    prod_path, prod_note = write_products_csv()

    # coverage: prove no source under assets/ was silently ignored (full runs)
    unhandled = []
    if not args.only:
        unhandled = [p for p in all_assets_music() if p not in claimed]
    # orphans: built JSON with no index entry (renamed/removed source)
    orphans = find_orphans(index, selected)
    if args.prune and orphans:
        for jp in orphans:
            os.remove(jp)

    print("\n" + "=" * 60)
    print(f"converted {total_pieces} pieces "
          f"({total_bad} with non-trivial bad bars), {len(all_skips)} skipped")
    if ds_skips:
        print(f"dropped (>{MAX_DOUBLE_STOP * 100:.0f}% double stops): {len(ds_skips)}")
        for out, reason in ds_skips:
            print(f"  - {out}  [{reason}]")
    if all_needs:
        print(f"iOS support needed across set: {sorted(all_needs)}")
    if auto_added:
        print(f"AUTO-ADDED from new folders (review metadata + pricing): "
              f"{len(auto_added)}")
    if review:
        print(f"NEEDS REVIEW (multi-movement / multi-meter, staging): {len(review)}")
        for f in review:
            print(f"  - {f}")
    if unhandled:
        print(f"⚠ UNHANDLED sources (not in any folder with music — move them into "
              f"a subfolder): {len(unhandled)}")
        for f in unhandled:
            print(f"  - {os.path.relpath(f, ASSETS)}")
    if orphans:
        verb = "pruned" if args.prune else "found (run --prune to delete)"
        print(f"orphan JSON {verb}: {len(orphans)}")
        for jp in orphans:
            print(f"  - {os.path.relpath(jp, SCORES)}")
    print(f"score-index.json updated (sha256 stamped for {n_hashed} scores)")
    if hash_missing:
        print(f"⚠ no sha256 (file missing on disk): {len(hash_missing)}")
        for fn in hash_missing:
            print(f"  - {fn}")
    print(f"scores-inventory.csv: {inv_n} rows")
    print(f"products.csv: {prod_note}")


if __name__ == "__main__":
    main()
