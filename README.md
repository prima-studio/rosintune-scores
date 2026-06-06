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
  (Suzuki/demo, where composers differ per piece).
- **Curation is preserved.** `score-index.json` is the source of truth for a
  piece's `stage` / `price` / `isFree` (and any hand-edited title/composer).
  Re-runs keep these; only brand-new pieces get folder defaults. New paid
  collections default to **staging** so nothing auto-publishes — publish
  deliberately by editing the entry's `stage` to `published`.
- **Copyright.** Only public-domain material. `SKIP_SUBSTR` excludes known
  in-copyright sources; the Suzuki-composed Book 1 pieces are intentionally
  absent.
- `assets/` is gitignored; the generated JSON + index + CSVs are committed.

## App Store products

`products.csv` is regenerated from the index (via
`scripts/generate-app-store-products.py`) — one Non-Consumable per paid piece,
product id `<collection>.<Title>`.

## Known limitations

- **Multi-movement works** (e.g. Bach Partita No. 2/3, Sonata No. 1) change meter
  per movement but the JSON has one `timeSignature`; they convert but most bars
  fail validation. Would need per-movement splitting / multi-meter support.
- **Renaming** a `named`/auto source can leave the old JSON behind (the logic that
  keeps externally-sourced scores like `example/twinkle.json` can't tell a rename
  from a legit external file). Numbered collections use stable slugs and are
  immune. Use `--prune` or tidy manually.
