#!/usr/bin/env python3
"""Generate a CSV for bulk-importing in-app purchases into App Store Connect.

Reads score-index.json, finds every paid score, and writes a CSV with the
product IDs that match the app's naming convention:

    {collectionID}/{scoreTitle}   (spaces stripped)

Usage:
    python3 scripts/generate-app-store-products.py > products.csv
"""

import json
import csv
import sys
import os
import unicodedata

INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "score-index.json")


def load_index():
    with open(INDEX_PATH) as f:
        return json.load(f)


def sanitize(text: str) -> str:
    """App Store Connect product IDs are ASCII [A-Za-z0-9._]. Fold accents to
    ASCII (Bourrée -> Bourree), drop anything else (em dash, spaces). Hyphens
    become underscores. NOTE: isalnum() alone keeps Unicode letters, which the
    App Store rejects -- the ascii round-trip is what enforces ASCII-only."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("-", "_")
    return "".join(c for c in text if c.isalnum() or c in "._")


def main():
    index = load_index()
    rows = []

    for col in index.get("collections", []):
        for score in col.get("scores", []):
            price = score.get("price")
            # Paid when the per-piece price is > 0. Fall back to the collection-level
            # isFree flag for collections that haven't adopted per-piece pricing.
            if price is not None:
                if not (price and float(price) > 0):
                    continue       # free piece -> no IAP product
            elif col.get("isFree", True):
                continue

            product_id = sanitize(col["id"]) + "." + sanitize(score["title"])
            rows.append({
                "Product ID": product_id,
                "Reference Name": f"{score['title']} ({col['title']})",
                "Product Type": "Non-Consumable",
                "Price": str(price) if price else "",   # per-piece price; blank = set in App Store Connect
                "Description": f"{score['title']} from {col['title']} by {col.get('composer', 'Unknown')}",
            })

    if not rows:
        print("No published paid scores found.", file=sys.stderr)
        sys.exit(0)

    writer = csv.DictWriter(sys.stdout, fieldnames=[
        "Product ID", "Reference Name", "Product Type", "Price", "Description"
    ])
    writer.writeheader()
    writer.writerows(rows)
    print(f"\n{len(rows)} products written.", file=sys.stderr)


if __name__ == "__main__":
    main()
