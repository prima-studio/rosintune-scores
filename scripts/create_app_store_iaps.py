#!/usr/bin/env python3
"""Create App Store Connect in-app purchases from products.csv.

Reads products.csv (Product ID, Reference Name, Product Type, Price, Description)
and, for each row, ensures a matching in-app purchase exists in App Store Connect:

    create IAP  ->  add en-US localization  ->  set the price schedule
                 (-> upload a review screenshot  -> submit for review)   [optional]

The run is IDEMPOTENT: products already present (matched by productId) are detected
and skipped, so it is safe to re-run as you add more etudes. It is also DRY-RUN BY
DEFAULT -- it prints the plan and touches nothing until you pass --apply.

This supersedes the fastlane `create_products` lane: fastlane's `produce` action
creates *app* records, not in-app purchases, so that lane never actually worked.

------------------------------------------------------------------------------
Auth (App Store Connect API key -- generate at App Store Connect -> Users and
Access -> Integrations -> App Store Connect API, role "App Manager"):

    export ASC_KEY_ID=ABCD1234XY
    export ASC_ISSUER_ID=11111111-2222-3333-4444-555555555555
    export ASC_KEY_PATH=~/.appstoreconnect/AuthKey_ABCD1234XY.p8

The .p8 stays on your machine; this script signs a short-lived JWT with it via the
`openssl` CLI (no Python packages required).

------------------------------------------------------------------------------
Usage:

    # Show what would happen -- no changes (dry run is the default):
    python3 scripts/create_app_store_iaps.py --app-id 6480000000

    # Look the app up by bundle id instead of numeric id:
    python3 scripts/create_app_store_iaps.py --bundle-id com.prima.RosinTune

    # Just one product first, for real:
    python3 scripts/create_app_store_iaps.py --app-id 6480000000 \
        --only kayser_36_studies.EtudeNo.2 --apply

    # Create + price everything for real:
    python3 scripts/create_app_store_iaps.py --app-id 6480000000 --apply

    # Also attach a review screenshot and submit each new IAP for review:
    python3 scripts/create_app_store_iaps.py --app-id 6480000000 --apply \
        --screenshot assets/iap-review.png --submit

    # List the IAPs already on the app and exit:
    python3 scripts/create_app_store_iaps.py --app-id 6480000000 --list
"""
import argparse
import base64
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from tqdm import tqdm  # optional: nicer progress bar for big runs / submissions
except ImportError:
    tqdm = None

API = "https://api.appstoreconnect.apple.com"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CSV = os.path.join(REPO_ROOT, "products.csv")

# App Store Connect field limits.
DISPLAY_NAME_MAX = 30    # localization name (customer-facing)
REFERENCE_NAME_MAX = 64  # inAppPurchases.name (internal)
DESCRIPTION_MAX = 55     # localization description (customer-facing)

PRODUCT_TYPE_MAP = {
    "non-consumable": "NON_CONSUMABLE",
    "consumable": "CONSUMABLE",
    "non-renewing-subscription": "NON_RENEWING_SUBSCRIPTION",
}


# --------------------------------------------------------------------------- #
# .env loader (no python-dotenv dependency -- keeps this script stdlib-only).
# Existing real environment variables win, matching python-dotenv's default.
# --------------------------------------------------------------------------- #
def load_env_file(path: str) -> None:
    if not path or not os.path.isfile(path):
        return
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            os.environ.setdefault(key, val)


# --------------------------------------------------------------------------- #
# JWT signing (ES256) using the openssl CLI -- avoids a PyJWT/cryptography dep.
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _der_to_raw_ecdsa(der: bytes, size: int = 32) -> bytes:
    """Convert an ASN.1 DER ECDSA signature (SEQUENCE{INTEGER r, INTEGER s})
    into the fixed-length r||s form that a JWS ES256 signature requires."""
    if not der or der[0] != 0x30:
        raise ValueError("malformed ECDSA signature (no SEQUENCE)")
    idx = 2
    if der[1] & 0x80:  # long-form length (not expected for P-256, but be safe)
        idx = 2 + (der[1] & 0x7F)

    def read_int(i):
        if der[i] != 0x02:
            raise ValueError("malformed ECDSA signature (expected INTEGER)")
        length = der[i + 1]
        start = i + 2
        return der[start:start + length].lstrip(b"\x00"), start + length

    r, j = read_int(idx)
    s, _ = read_int(j)
    return r.rjust(size, b"\x00") + s.rjust(size, b"\x00")


def make_jwt(key_id: str, issuer_id: str, key_path: str) -> str:
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 20 * 60,  # Apple caps token lifetime at 20 minutes.
        "aud": "appstoreconnect-v1",
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + b"."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    try:
        der = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", os.path.expanduser(key_path)],
            input=signing_input,
            capture_output=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("error: `openssl` not found on PATH (needed to sign the JWT)")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"error: openssl failed to sign with {key_path}:\n{exc.stderr.decode()}")
    return (signing_input + b"." + _b64url(_der_to_raw_ecdsa(der))).decode()


# --------------------------------------------------------------------------- #
# Tiny App Store Connect REST client (stdlib urllib).
# --------------------------------------------------------------------------- #
class ASCError(Exception):
    def __init__(self, method, url, code, detail):
        self.method, self.url, self.code, self.detail = method, url, code, detail
        super().__init__(f"{method} {url} -> HTTP {code}\n{detail}")


class ASC:
    def __init__(self, key_id, issuer_id, key_path):
        self._key = (key_id, issuer_id, key_path)
        self._token = None
        self._issued = 0.0
        self._territories = None  # cached list of all territory ids

    @property
    def token(self) -> str:
        # Refresh well before the 20-minute cap so long runs don't expire mid-flight.
        if self._token is None or (time.time() - self._issued) > 15 * 60:
            self._token = make_jwt(*self._key)
            self._issued = time.time()
        return self._token

    def _request(self, method, path, body=None, query=None):
        url = path if path.startswith("http") else API + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise ASCError(method, url, exc.code, exc.read().decode(errors="replace")) from None

    def paged(self, path, query=None):
        page = self._request("GET", path, query=query)
        while True:
            yield from page.get("data", [])
            nxt = page.get("links", {}).get("next")
            if not nxt:
                break
            page = self._request("GET", nxt)

    # -- app lookup ------------------------------------------------------- #
    def resolve_app_id(self, app_id=None, bundle_id=None) -> str:
        if app_id:
            return app_id
        if not bundle_id:
            sys.exit("error: pass --app-id or --bundle-id")
        apps = list(self.paged("/v1/apps", {"filter[bundleId]": bundle_id, "limit": 200}))
        match = next((a for a in apps if a["attributes"]["bundleId"] == bundle_id), None)
        if not match:
            sys.exit(f"error: no app found for bundle id {bundle_id}")
        return match["id"]

    # -- in-app purchases ------------------------------------------------- #
    def existing_iaps(self, app_id) -> dict:
        """Map productId -> {id, state} for every IAP already on the app."""
        out = {}
        for iap in self.paged(f"/v1/apps/{app_id}/inAppPurchasesV2", {"limit": 200}):
            out[iap["attributes"]["productId"]] = {
                "id": iap["id"],
                "state": iap["attributes"].get("state"),
            }
        return out

    def get_state(self, iap_id) -> str:
        """Fresh review state for an IAP (e.g. MISSING_METADATA, READY_TO_SUBMIT,
        WAITING_FOR_REVIEW, IN_REVIEW, APPROVED)."""
        resp = self._request(
            "GET", f"/v2/inAppPurchases/{iap_id}", query={"fields[inAppPurchases]": "state"}
        )
        return resp["data"]["attributes"].get("state")

    def has_review_screenshot(self, iap_id) -> bool:
        """True when the IAP already carries a review screenshot (to-one), so we
        don't upload a duplicate on re-runs."""
        try:
            resp = self._request("GET", f"/v2/inAppPurchases/{iap_id}/appStoreReviewScreenshot")
        except ASCError as exc:
            if exc.code == 404:
                return False
            raise
        return bool(resp.get("data"))

    def has_localization(self, iap_id, locale) -> bool:
        """True when the IAP already has a localization for `locale`."""
        for loc in self.paged(f"/v2/inAppPurchases/{iap_id}/inAppPurchaseLocalizations"):
            if loc["attributes"].get("locale") == locale:
                return True
        return False

    def has_price_schedule(self, iap_id) -> bool:
        """True when the IAP already has a price schedule (i.e. a price set)."""
        try:
            resp = self._request("GET", f"/v2/inAppPurchases/{iap_id}/iapPriceSchedule")
        except ASCError as exc:
            if exc.code == 404:
                return False
            raise
        return bool(resp.get("data"))

    def has_availability(self, iap_id) -> bool:
        """True when the IAP already has a territory availability set. Without one
        the IAP is stuck in MISSING_METADATA even with name + price + screenshot."""
        try:
            resp = self._request("GET", f"/v2/inAppPurchases/{iap_id}/inAppPurchaseAvailability")
        except ASCError as exc:
            if exc.code == 404:
                return False
            raise
        return bool(resp.get("data"))

    def territory_ids(self) -> list:
        """All App Store territory ids (cached); fetched once per run."""
        if self._territories is None:
            self._territories = [t["id"] for t in self.paged("/v1/territories", {"limit": 200})]
        return self._territories

    def set_availability(self, iap_id, available_in_new=True) -> None:
        """Make the IAP available in every territory (prices derive from the base
        territory's price point) and opt in to territories Apple adds later."""
        body = {
            "data": {
                "type": "inAppPurchaseAvailabilities",
                "attributes": {"availableInNewTerritories": available_in_new},
                "relationships": {
                    "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                    "availableTerritories": {
                        "data": [{"type": "territories", "id": t} for t in self.territory_ids()]
                    },
                },
            }
        }
        self._request("POST", "/v1/inAppPurchaseAvailabilities", body)

    def create_iap(self, app_id, product_id, name, iap_type, review_note=None) -> str:
        attrs = {"name": name, "productId": product_id, "inAppPurchaseType": iap_type}
        if review_note:
            attrs["reviewNote"] = review_note
        body = {
            "data": {
                "type": "inAppPurchases",
                "attributes": attrs,
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        }
        return self._request("POST", "/v2/inAppPurchases", body)["data"]["id"]

    def add_localization(self, iap_id, locale, name, description) -> None:
        attrs = {"locale": locale, "name": name}
        if description:
            attrs["description"] = description
        body = {
            "data": {
                "type": "inAppPurchaseLocalizations",
                "attributes": attrs,
                "relationships": {
                    "inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id}}
                },
            }
        }
        self._request("POST", "/v1/inAppPurchaseLocalizations", body)

    def find_price_point(self, iap_id, territory, price) -> str:
        """Return the inAppPurchasePricePoint id whose customer price == `price`
        in `territory`. Apple derives every other territory from this base point."""
        target = round(float(price), 2)
        for pp in self.paged(
            f"/v2/inAppPurchases/{iap_id}/pricePoints",
            {"filter[territory]": territory, "limit": 200},
        ):
            if round(float(pp["attributes"]["customerPrice"]), 2) == target:
                return pp["id"]
        raise ValueError(
            f"no {territory} price point equal to {target:.2f} for IAP {iap_id}"
        )

    def set_price(self, iap_id, territory, price_point_id) -> None:
        """Set the whole price schedule at once with a single base price whose
        startDate is null (= the live price), per Apple's price-schedule rules.
        The inline price uses a local id in the required ${...} form."""
        tmp = "${price-base}"
        body = {
            "data": {
                "type": "inAppPurchasePriceSchedules",
                "relationships": {
                    "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                    "baseTerritory": {"data": {"type": "territories", "id": territory}},
                    "manualPrices": {"data": [{"type": "inAppPurchasePrices", "id": tmp}]},
                },
            },
            "included": [
                {
                    "type": "inAppPurchasePrices",
                    "id": tmp,
                    "attributes": {"startDate": None},
                    "relationships": {
                        "inAppPurchasePricePoint": {
                            "data": {"type": "inAppPurchasePricePoints", "id": price_point_id}
                        }
                    },
                }
            ],
        }
        self._request("POST", "/v1/inAppPurchasePriceSchedules", body)

    # -- review screenshot + submission (optional) ------------------------ #
    def upload_review_screenshot(self, iap_id, image_path) -> None:
        blob = open(image_path, "rb").read()
        reserve = self._request(
            "POST",
            "/v1/inAppPurchaseAppStoreReviewScreenshots",
            {
                "data": {
                    "type": "inAppPurchaseAppStoreReviewScreenshots",
                    "attributes": {
                        "fileName": os.path.basename(image_path),
                        "fileSize": len(blob),
                    },
                    "relationships": {
                        "inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id}}
                    },
                }
            },
        )["data"]
        for op in reserve["attributes"]["uploadOperations"]:
            chunk = blob[op["offset"]: op["offset"] + op["length"]]
            req = urllib.request.Request(op["url"], data=chunk, method=op["method"])
            for header in op.get("requestHeaders", []):
                req.add_header(header["name"], header["value"])
            urllib.request.urlopen(req).read()
        self._request(
            "PATCH",
            f"/v1/inAppPurchaseAppStoreReviewScreenshots/{reserve['id']}",
            {
                "data": {
                    "type": "inAppPurchaseAppStoreReviewScreenshots",
                    "id": reserve["id"],
                    "attributes": {
                        "uploaded": True,
                        "sourceFileChecksum": hashlib.md5(blob).hexdigest(),
                    },
                }
            },
        )

    def submit_for_review(self, iap_id) -> None:
        self._request(
            "POST",
            "/v1/inAppPurchaseSubmissions",
            {
                "data": {
                    "type": "inAppPurchaseSubmissions",
                    "relationships": {
                        "inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id}}
                    },
                }
            },
        )


# --------------------------------------------------------------------------- #
# CSV -> product rows
# --------------------------------------------------------------------------- #
def clip(text: str, limit: int) -> str:
    """Trim `text` to `limit` chars, preferring a word boundary so we don't leave
    a chopped word (App Store Connect rejects over-long names/descriptions)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    head = cut.rsplit(" ", 1)[0]
    # Use the word boundary only if it keeps most of the budget; else hard-cut.
    return (head if " " in cut and len(head) >= limit * 0.6 else cut).rstrip()


def display_name(reference_name: str) -> str:
    """Customer-facing name: the part before ' (' (e.g. 'Etude No. 2'), trimmed
    to App Store Connect's 30-char limit."""
    base = reference_name.split(" (", 1)[0].strip() or reference_name.strip()
    return clip(base, DISPLAY_NAME_MAX)


def read_products(csv_path, default_price):
    rows = []
    with open(csv_path, newline="") as fh:
        for line in csv.DictReader(fh):
            product_id = (line.get("Product ID") or "").strip()
            if not product_id:
                continue
            ref_name = (line.get("Reference Name") or product_id).strip()[:REFERENCE_NAME_MAX]
            type_key = (line.get("Product Type") or "Non-Consumable").strip().lower()
            iap_type = PRODUCT_TYPE_MAP.get(type_key, "NON_CONSUMABLE")
            raw_price = (line.get("Price") or "").strip()
            price = float(raw_price) if raw_price else float(default_price)
            rows.append({
                "product_id": product_id,
                "reference_name": ref_name,
                "display_name": display_name(ref_name),
                "type": iap_type,
                "price": price,
                "price_defaulted": not raw_price,
                "description": clip(line.get("Description") or "", DESCRIPTION_MAX),
            })
    return rows


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Create App Store Connect in-app purchases from products.csv.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--csv", default=DEFAULT_CSV, help=f"products CSV (default: {DEFAULT_CSV})")
    ap.add_argument("--env-file", default=os.path.join(REPO_ROOT, ".env"),
                    help="dotenv file to load (default: <repo>/.env; real env vars win)")
    ap.add_argument("--app-id", help="numeric app id (or ASC_APP_ID in the environment)")
    ap.add_argument("--bundle-id",
                    help="look the app up by bundle id (or ASC_BUNDLE_ID in the environment)")
    ap.add_argument("--territory", default="USA", help="base territory for pricing (default: USA)")
    ap.add_argument("--locale", default="en-US", help="localization locale (default: en-US)")
    ap.add_argument("--default-price", default="1.99",
                    help="price for rows with a blank Price column (default: 1.99)")
    ap.add_argument("--only", action="append", default=[],
                    help="only this productId (repeatable); default is every row")
    ap.add_argument("--screenshot",
                    help="image to attach as the review screenshot (new + existing IAPs "
                         "lacking one); skipped if the IAP already has one")
    ap.add_argument("--submit", action="store_true",
                    help="submit every Ready-to-Submit IAP for review (needs --screenshot)")
    ap.add_argument("--list", action="store_true",
                    help="list the app's existing IAPs and exit")
    ap.add_argument("--progress", action="store_true",
                    help="show a tqdm progress bar instead of per-product output "
                         "(needs `pip install tqdm`); failures still print")
    ap.add_argument("--apply", action="store_true",
                    help="actually create things (default is a dry run)")
    args = ap.parse_args()
    load_env_file(args.env_file)

    key_id = os.environ.get("ASC_KEY_ID")
    issuer_id = os.environ.get("ASC_ISSUER_ID")
    key_path = os.environ.get("ASC_KEY_PATH")
    if not (key_id and issuer_id and key_path):
        sys.exit("error: set ASC_KEY_ID, ASC_ISSUER_ID, and ASC_KEY_PATH (see --help)")
    if args.submit and not args.screenshot:
        sys.exit("error: --submit requires --screenshot (Apple needs a review screenshot)")

    app_id_arg = args.app_id or os.environ.get("ASC_APP_ID")
    bundle_id_arg = args.bundle_id or os.environ.get("ASC_BUNDLE_ID")

    asc = ASC(key_id, issuer_id, key_path)
    app_id = asc.resolve_app_id(app_id_arg, bundle_id_arg)
    existing = asc.existing_iaps(app_id)

    if args.list:
        print(f"App {app_id} has {len(existing)} in-app purchase(s):")
        for pid in sorted(existing):
            print(f"  {pid}")
        return

    rows = read_products(args.csv, args.default_price)
    if args.only:
        wanted = set(args.only)
        rows = [r for r in rows if r["product_id"] in wanted]
    if not rows:
        sys.exit("error: no matching rows in the CSV")

    mode = "APPLY" if args.apply else "DRY RUN (no changes -- pass --apply to create)"
    print(f"App id: {app_id}   territory: {args.territory}   mode: {mode}")
    print(f"{len(rows)} product(s) from {args.csv}\n")

    # States that mean "already in or past review" -- don't re-submit these.
    IN_REVIEW_OR_DONE = {
        "WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE",
        "PENDING_BINARY_APPROVAL", "APPROVED", "READY_FOR_SALE",
        "DEVELOPER_REMOVED_FROM_SALE", "REMOVED_FROM_SALE",
    }
    want_shot, want_submit = bool(args.screenshot), bool(args.submit)
    n_create = n_local = n_price = n_avail = n_shot = n_submit = n_fail = 0

    use_progress = bool(args.progress) and tqdm is not None
    if args.progress and tqdm is None:
        print("note: tqdm not installed -- `pip install tqdm` for the bar; "
              "continuing without it.", file=sys.stderr)

    def emit(*msgs):
        """Print lines without corrupting an active tqdm bar."""
        for msg in msgs:
            tqdm.write(msg) if use_progress else print(msg)

    bar = (tqdm(rows, unit="iap", desc=("submit" if want_submit else "reconcile"))
           if use_progress else None)

    for row in (bar if use_progress else rows):
        pid = row["product_id"]
        price_note = "  [price defaulted]" if row["price_defaulted"] else ""
        is_existing = pid in existing
        iap_id = existing[pid]["id"] if is_existing else None
        cached_state = existing[pid]["state"] if is_existing else None

        header = (f"  = {pid}  (exists, state={cached_state})" if is_existing
                  else f"  + {pid}  ${row['price']:.2f}  \"{row['display_name']}\"{price_note}")
        detail, failed = "", False

        try:
            # What's missing? New products need everything; existing ones are
            # probed so we repair only the gaps (price OR localization OR shot).
            need_local = (not is_existing) or not asc.has_localization(iap_id, args.locale)
            need_price = (not is_existing) or not asc.has_price_schedule(iap_id)
            need_avail = (not is_existing) or not asc.has_availability(iap_id)
            need_shot = want_shot and ((not is_existing) or not asc.has_review_screenshot(iap_id))

            # -------- dry run: report intended actions, change nothing -------- #
            if not args.apply:
                actions = []
                if not is_existing:
                    actions.append("create")
                if need_local:
                    actions.append("localize")
                if need_price:
                    actions.append("price")
                if need_avail:
                    actions.append("availability")
                if want_shot:
                    actions.append("screenshot" if need_shot else "screenshot present")
                if want_submit:
                    # After the repairs above it should reach READY_TO_SUBMIT.
                    actions.append(f"already in/through review ({cached_state})"
                                   if cached_state in IN_REVIEW_OR_DONE else "submit for review")
                if not is_existing:
                    n_create += 1
                n_local += int(need_local)
                n_price += int(need_price)
                n_avail += int(need_avail)
                n_shot += int(need_shot)
                n_submit += int(want_submit and cached_state not in IN_REVIEW_OR_DONE)
                detail = "      would: " + ("; ".join(actions) or "nothing to do")

            # ------------------------- apply ------------------------- #
            else:
                done = []
                if not is_existing:
                    iap_id = asc.create_iap(
                        app_id, pid, row["reference_name"], row["type"],
                        review_note=row["description"] or None,
                    )
                    existing[pid] = {"id": iap_id, "state": "MISSING_METADATA"}
                    n_create += 1
                    done.append("created")
                if need_local:
                    asc.add_localization(iap_id, args.locale, row["display_name"], row["description"])
                    n_local += 1
                    done.append("localized")
                if need_price:
                    point_id = asc.find_price_point(iap_id, args.territory, row["price"])
                    asc.set_price(iap_id, args.territory, point_id)
                    n_price += 1
                    done.append("priced")
                if need_avail:
                    asc.set_availability(iap_id)
                    n_avail += 1
                    done.append("availability set")
                if want_shot:
                    if need_shot:
                        asc.upload_review_screenshot(iap_id, args.screenshot)
                        n_shot += 1
                        done.append("screenshot uploaded")
                    else:
                        done.append("screenshot present")
                if want_submit:
                    state = asc.get_state(iap_id)
                    if state == "READY_TO_SUBMIT":
                        asc.submit_for_review(iap_id)
                        n_submit += 1
                        done.append("submitted")
                    elif state in IN_REVIEW_OR_DONE:
                        done.append(f"already in/through review ({state})")
                    else:
                        done.append(f"NOT submittable (state={state})")
                detail = f"      {', '.join(done) or 'nothing to do'}  [{iap_id}]"
        except (ASCError, ValueError) as exc:
            failed, n_fail = True, n_fail + 1
            detail = f"      FAILED: {exc}"

        if use_progress:
            bar.set_postfix(new=n_create, price=n_price, shot=n_shot,
                            sub=n_submit, fail=n_fail, refresh=False)
            if failed:  # surface failures even when the bar hides the detail
                emit(header, detail)
        else:
            emit(header, detail)

    if bar is not None:
        bar.close()

    verb = "would " if not args.apply else ""
    print(f"\nDone. {verb}create: {n_create}   localize: {n_local}   price: {n_price}   "
          f"availability: {n_avail}   screenshot: {n_shot}   submit: {n_submit}   failed: {n_fail}")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
