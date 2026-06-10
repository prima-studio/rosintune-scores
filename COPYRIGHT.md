# Copyright policy & provenance

Every piece in this catalog is a **public-domain musical composition**, and the
score data in this repository reproduces **no copyrighted edition's pages,
typesetting, or editorial content**. This document is the policy and the
per-collection rationale; the machine-generated piece-by-piece manifest lives
in `appstore-evidence/` (regenerate with `scripts/build_evidence.py`).

## The two layers of sheet-music copyright

1. **The composition** (notes, rhythms, the composer's slurs/articulations/
   dynamics). Public domain once the composer has been dead long enough —
   every composer in this catalog died in **1888 or earlier**, and every work
   was first published before 1900. That is public domain in the United States
   (first published before 1930) and in every life-plus-70 (and even
   life-plus-100) jurisdiction.

2. **The edition** (a publisher's engraving/typesetting and an editor's added
   fingerings, bowings, phrasing). Modern editions of public-domain works can
   carry their own rights — which is why this repo never reproduces one:
   - The app renders all notation at runtime with **its own engraving engine**
     from the JSON in `scores/`. No scans or images of any printed edition
     exist anywhere in the pipeline.
   - Note data is converted by `scripts/musicxml_to_score.py` from
     transcriptions of the public-domain works. A faithful transcription adds
     no new copyright to the musical content — a transcriber cannot own
     Kayser's notes.
   - For the beginner collections (`example`, `first-folk-tunes`) the
     converter runs with `--mechanical-editorial`: **all** fingerings, string
     numbers, and bowing directions are machine-generated (standard
     first-position table + alternating bow strokes), and source slurs/
     articulations are dropped. Zero third-party editorial content, verified
     by audit.
   - For the étude collections (Kayser, Kreutzer, Rode, Bach) the slurs and
     articulations **are the composition** — those studies exist to teach
     exactly those bowing patterns, and they are the composers' own text.
     Fingerings are overwhelmingly the converter's mechanical first-position
     map; the rare source-supplied fingering is a standard, non-creative
     position assignment.

## Per-collection rationale

| Collection | Composer(s) | Died | First published | Basis |
|---|---|---|---|---|
| example | Traditional; L. van Beethoven | — / 1827 | pre-1850 | Folk melodies; Symphony No. 9 (1824) |
| first-folk-tunes | Traditional; J.A.P. Schulz (1747–1800); T.H. Bayly (1797–1839); R. Schumann (1810–1856); J.S. Bach (1685–1750); F.J. Gossec (1734–1829) | ≤1856 | pre-1850 | Folk melodies and short classics |
| kayser-36-studies | H.E. Kayser (1815–1888) | 1888 | c. 1848 | 36 Elementary & Progressive Studies, Op. 20 |
| kreutzer-42-studies | R. Kreutzer (1766–1831) | 1831 | 1796 | 42 Études ou Caprices |
| rode-24-caprices | Pierre Rode (1774–1830) | 1830 | c. 1815 | 24 Caprices, Op. 22 |
| bach-for-violin, bach-partita, bach-partita-3, bach-sonatas-partitas | J.S. Bach (1685–1750) | 1750 | composed 1720 | Sonatas & Partitas BWV 1001–1006 and arrangements of PD works |

## Operating rules

- **No in-copyright compositions.** `SKIP_SUBSTR` in `scripts/build_scores.py`
  excludes known in-copyright sources (e.g. Gardel's *Por una Cabeza*), and
  20th-century pedagogical compositions are never included.
- **No copyrighted edition's engraving.** Sources known to be a specific
  arranger's copyrighted edition are skipped (e.g. the 王振山 Kayser edition).
- **No method-book or edition branding.** Collections are named after
  composers and works ("Kayser 36 Studies"), never after copyrighted method
  books, publishers, or editions.
- **Publish gate.** Before flipping a piece's `stage` to `published`, the
  editorial review should also confirm the transcription is faithful to the
  public-domain work and free of a modern edition's added markings.
