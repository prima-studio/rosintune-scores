# rosintune-scores

Violin sheet-music data for the RosinTune iOS app. Source files (MusicXML) live
under `assets/` and are converted into the app's score JSON under `scores/`.

## Layout

```
assets/violin/<folder>/…   source MusicXML (.mxl/.musicxml/.xml)  — gitignored
scores/violin/<collection>/*.json   generated scores (committed)
score-index.json           catalog: collections + per-piece stage/price/isFree
products.csv               App Store IAP import (paid pieces)        — generated
scores-inventory.csv       full human-readable catalog of every score — generated
scripts/                   the pipeline
```

## The pipeline — `scripts/build_scores.py`

One command converts **every** source under `assets/` into score JSON, rebuilds
`score-index.json`, and regenerates both CSVs.

```bash
python3 scripts/build_scores.py            # build everything
python3 scripts/build_scores.py --dry-run  # preview plan + skips + coverage; write nothing
python3 scripts/build_scores.py --only kreutzer-42-studies suzuki-book-1
python3 scripts/build_scores.py --prune    # also delete orphaned JSON (renamed/removed sources)
```

### Adding new scores (the common case)

1. Drop the MusicXML into `assets/violin/<folder>/`.
2. Run `python3 scripts/build_scores.py`.

- A file added to a **known folder** is picked up automatically.
- A brand-**new folder** is **auto-discovered** and added as its own collection
  (id/title from the folder name, composer read from the source, **$0.99 /
  staging**, flagged `AUTO-ADDED`). For clean metadata, promote it into the
  `FOLDERS` table in `build_scores.py`.

The run is **idempotent** — re-running changes nothing unless sources changed.

### What the run reports

- **skipped** — copyright (`SKIP_SUBSTR`) or duplicate sources (kept the cleanest
  violin one per numbered étude).
- **dropped (double stops)** — a converted piece whose double-stop (chord) notes
  exceed `MAX_DOUBLE_STOP` (10% of sounding notes) is removed: too chord-heavy for
  the single-line reader. Its JSON is deleted and it gets no index entry. (A
  previously *published* drop is flagged loudly.)
- **NEEDS REVIEW** — multi-movement / multi-meter works (one global time signature
  can't model them); kept in staging.
- **AUTO-ADDED** — pieces from a newly discovered folder; review their metadata.
- **UNHANDLED** — any source it couldn't place (shouldn't happen; nothing is
  silently dropped).
- **orphan JSON** — generated files with no index entry; `--prune` deletes them.

## Conventions

- **Folders → collections** are defined in the `FOLDERS` table in
  `build_scores.py`. `number` scheme = numbered études/caprices ("Etude No. N");
  `named` = title from filename (Bach); `table` = explicit per-piece metadata
  (Suzuki/demo, where composers differ per piece); `movements` = split one
  multi-movement source into one JSON per movement (the 6 Sonatas & Partitas).
- **Curation is preserved.** `score-index.json` is the source of truth for a
  piece's `stage` / `price` / `isFree` (and any hand-edited title/composer).
  Re-runs keep these; only brand-new pieces get folder defaults. New paid
  collections default to **staging** so nothing auto-publishes — publish
  deliberately by editing the entry's `stage` to `published`.
- **Copyright.** Only public-domain material. `SKIP_SUBSTR` excludes known
  in-copyright sources; the Suzuki-composed Book 1 pieces are intentionally
  absent.
- **Double stops.** Pieces with >10% double-stop notes (`MAX_DOUBLE_STOP`) are
  dropped after conversion — too chord-heavy for the reader. This removes the
  chord études (e.g. Kreutzer 33/37/42, Kayser 20) and the chordal Bach movements
  (fugues, slow movements, Ciaccona), keeping the single-line ones.
- `assets/` is gitignored; the generated JSON + index + CSVs are committed.

## Reviewing staging pieces (the publish gate)

The app repo ships a review harness that validates every generated JSON with
the app's **production decoder** (decode, pitch parsing, repeat reachability,
sha256 match, orphan detection) and renders each piece's full engraving as a
page of stacked systems:

```bash
cd ../RosinTune
ROSINTUNE_SCORES_DIR=$(pwd)/../rosintune-scores \
    swift test --filter ScoreRepoReviewTests
open ../rosintune-scores/review-gallery   # flip through the PNGs
```

`review-gallery/report.csv` lists every piece with bar counts, beats-vs-meter
bad bars, and render status. Review the images, then publish by flipping the
entry's `stage` to `published` in `score-index.json` (curation is preserved
across rebuilds).

`sha256` fields are stamped automatically on every `build_scores.py` run; the
app rejects downloads that don't match, so always commit the regenerated index
together with changed score JSON.

## App Store products

`products.csv` is regenerated from the index (via
`scripts/generate-app-store-products.py`) — one Non-Consumable per paid piece,
product id `<collection>.<Title>`.

## Multi-movement works (Sonatas & Partitas)

The 6 solo Sonatas & Partitas (BWV 1001–1006) are multi-movement: each movement
has its own meter, which a single-`timeSignature` JSON can't model. The
`movements` scheme handles this — `assets/violin/bach-sonatas-partitas/*.mxl` is
split into one JSON per movement (boundaries detected where a measure declares an
explicit `<time>` together with a `<words>` heading like *Adagio* / *Fuga* /
*Ciaccona*). Each movement file gets the correct single meter, the work key
(parsed from the filename, since the Baroque sources use one-flat-short
signatures that defeat auto-detection), and a title like
`Sonata No. 1 in G Minor — IV. Presto`. Filenames are `bwv<NNNN>-<MM>-<slug>.json`.
The collection is `bach-sonatas-partitas` (32 movements, paid/staging).

## Known limitations

- **Dense polyphonic movements.** The slow, chord-heavy movements (Adagio, Grave,
  Largo, several Allemandas) reduce 3–4 simultaneous voices to one melodic line by
  onset, so some bars don't sum exactly to the meter; these are flagged
  `NEEDS REVIEW` and kept in staging. Fast/dance movements convert cleanly. (Some
  flagged bars are benign split bars at repeat boundaries.)
- **Renaming** a `named`/auto source can leave the old JSON behind (the logic that
  keeps externally-sourced scores like `example/twinkle.json` can't tell a rename
  from a legit external file). Numbered collections use stable slugs and are
  immune. Use `--prune` or tidy manually.
