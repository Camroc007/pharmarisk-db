import json
import re
import sqlite3
import sys
import urllib.request

# ---------------------------------------------------------------------------
# 1. Configuration & static fallback data
# ---------------------------------------------------------------------------
# NOTE: this endpoint currently returns HTTP 404, so the static map below is
# what actually drives the risk flags. Replace with a real data source.
MHRA_YELLOW_CARD_URL = "https://products.mhra.gov.uk/api/idap/summary"

# Maps a dm+d (UK) name to the name used in the risk database (international).
DRUG_SYNONYMS = {
    "hyoscine butylbromide": "scopolamine butylbromide",
    "hyoscine hydrobromide": "scopolamine hydrobromide",
    "hyoscine": "scopolamine",
    "adrenaline": "epinephrine",
    "noradrenaline": "norepinephrine",
    "furosemide": "frusemide",
    "torasemide": "torsemide",
    "bendroflumethiazide": "bendrofluazide",
    "dexamfetamine": "dexamphetamine",
}

STATIC_CARDIAC_RISK_MAP = {
    "hyoscine butylbromide": ["Arrhythmia", "Tachycardia", "Palpitations"],
    "hyoscine hydrobromide": ["Arrhythmia", "Tachycardia", "Palpitations"],
    "pseudoephedrine": ["Tachycardia", "Arrhythmia", "Palpitations", "Hypertension"],
    "ephedrine": ["Tachycardia", "Arrhythmia", "Hypertension"],
    "citalopram": ["QT Prolongation", "Tachycardia", "Arrhythmia"],
    "escitalopram": ["QT Prolongation", "Tachycardia"],
    "amiodarone": ["Arrhythmia", "Bradycardia", "QT Prolongation", "Tachycardia"],
    "sotalol": ["Bradycardia", "QT Prolongation", "Arrhythmia"],
    "erythromycin": ["QT Prolongation", "Tachycardia", "Arrhythmia"],
    "domperidone": ["QT Prolongation", "Arrhythmia", "Ventricular Arrhythmia"],
}

SALTS_AND_SUFFIXES = (
    r"\b(ph\.?\s*eur|usp|bp|hydrate|hydrochloride|hcl|maleate|sodium|sulfate|"
    r"sulphate|besilate|mesilate|tartrate|trihydrate|dihydrate|phosphate|"
    r"acetate|bromide)\b"
)


# ---------------------------------------------------------------------------
# 2. Matching
# ---------------------------------------------------------------------------
def normalize_text(text):
    if not text:
        return ""
    clean = text.lower()
    clean = re.sub(SALTS_AND_SUFFIXES, "", clean)
    clean = re.sub(r"[^a-z0-9\s]", " ", clean)
    return " ".join(clean.split())


def find_mhra_match(dmd_ingredient, mhra_db):
    """Exact, synonym, then whole-word match only.

    No fuzzy or substring matching: for drug names that produces clinically
    wrong matches (e.g. carbamazepine vs oxcarbazepine, ephedrine vs
    pseudoephedrine).
    """
    norm = normalize_text(dmd_ingredient)
    if not norm:
        return None

    if norm in mhra_db:
        return mhra_db[norm]

    syn = DRUG_SYNONYMS.get(norm)
    if syn and syn in mhra_db:
        return mhra_db[syn]

    # e.g. "citalopram hydrobromide" -> key "citalopram": every word of the
    # key must appear as a whole word in the ingredient name.
    tokens = set(norm.split())
    best, best_len = None, 0
    for key, data in mhra_db.items():
        key_tokens = key.split()
        if key_tokens and set(key_tokens) <= tokens and len(key_tokens) > best_len:
            best, best_len = data, len(key_tokens)
    return best


# ---------------------------------------------------------------------------
# 3. Risk database (live fetch with static fallback)
# ---------------------------------------------------------------------------
def build_mhra_ingredient_database():
    print("Building MHRA Master Ingredient Database...")
    db = {}

    try:
        req = urllib.request.Request(
            MHRA_YELLOW_CARD_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            records = json.loads(response.read().decode("utf-8"))

        for item in records:
            raw_ing = item.get("substance_name", "")
            key = normalize_text(raw_ing)
            reaction = (item.get("reaction_term") or "").strip()
            soc = (item.get("soc_term") or "").strip()
            if not (key and reaction):
                continue

            entry = db.setdefault(key, {
                "ingredient_name": raw_ing.title(),
                "has_cardiac_risk": False,
                "cardiac_effects": set(),
                "all_side_effects": set(),
            })
            entry["all_side_effects"].add(reaction)

            cardiac_terms = ("arrhythmia", "tachycardia", "qt prolongation",
                             "palpitations", "bradycardia", "cardiac arrest",
                             "ventricular")
            if "cardiac" in soc.lower() or any(t in reaction.lower() for t in cardiac_terms):
                entry["has_cardiac_risk"] = True
                entry["cardiac_effects"].add(reaction)

        print(f"Loaded live MHRA data for {len(db)} ingredients.")
    except Exception as e:
        print(f"Notice: live MHRA fetch skipped ({e}). Using static cardiac risk rules only.")

    for name, terms in STATIC_CARDIAC_RISK_MAP.items():
        key = normalize_text(name)
        entry = db.setdefault(key, {
            "ingredient_name": name.title(),
            "has_cardiac_risk": True,
            "cardiac_effects": set(),
            "all_side_effects": set(),
        })
        entry["has_cardiac_risk"] = True
        entry["cardiac_effects"].update(terms)
        entry["all_side_effects"].update(terms)

    return db


# ---------------------------------------------------------------------------
# 4. Export
# ---------------------------------------------------------------------------
INGREDIENT_QUERY = """
SELECT DISTINCT vi.VPID, i.NM
FROM VMP__VIRTUAL_PRODUCT_INGREDIENT vi
JOIN INGREDIENT i ON i.ISID = vi.ISID
WHERE i.NM IS NOT NULL
ORDER BY vi.VPID, i.NM;
"""

GTIN_QUERY = """
SELECT g.GTIN, ampp.NM, amp.VPID
FROM GTIN__AMPP g
JOIN AMPP ampp ON ampp.APPID = g.AMPPID
LEFT JOIN AMP amp ON amp.APID = ampp.APID
WHERE g.GTIN IS NOT NULL AND g.GTIN != ''
ORDER BY g.GTIN;
"""


def dump_schema(conn):
    print("---- Actual database schema ----", file=sys.stderr)
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ):
        print(f"-- {name}\n{sql}\n", file=sys.stderr)


def build_gtin_json():
    mhra = build_mhra_ingredient_database()
    conn = sqlite3.connect("dmd.db")

    try:
        ingredients_by_vmp = {}
        for vpid, nm in conn.execute(INGREDIENT_QUERY):
            ingredients_by_vmp.setdefault(vpid, []).append(nm)
        rows = conn.execute(GTIN_QUERY).fetchall()
    except sqlite3.Error as e:
        print(f"SQLite query error: {e}", file=sys.stderr)
        dump_schema(conn)
        sys.exit(1)

    if not rows:
        print("Query returned no rows; not overwriting existing dataset.", file=sys.stderr)
        dump_schema(conn)
        sys.exit(1)

    dataset = []
    for gtin, product_name, vpid in rows:
        ingredients = ingredients_by_vmp.get(vpid, [])

        cardiac, side_effects, matched = set(), set(), []
        has_risk = False
        for ing in ingredients:
            m = find_mhra_match(ing, mhra)
            if not m:
                continue
            matched.append(ing)
            has_risk = has_risk or m["has_cardiac_risk"]
            cardiac.update(m["cardiac_effects"])
            side_effects.update(sorted(m["all_side_effects"])[:10])

        dataset.append({
            "gtin": str(gtin),
            "product_name": product_name or "",
            "ingredients": " + ".join(ingredients),
            "ingredients_with_risk_data": matched,
            "has_cardiac_risk": has_risk,
            "cardiac_effects": sorted(cardiac),
            "side_effects_summary": sorted(side_effects),
        })

    with open("dmd_gtin_latest.json", "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)

    ingredient_export = {
        k: {
            "ingredient_name": v["ingredient_name"],
            "has_cardiac_risk": v["has_cardiac_risk"],
            "cardiac_effects": sorted(v["cardiac_effects"]),
            "side_effects": sorted(v["all_side_effects"])[:15],
        }
        for k, v in mhra.items()
    }
    with open("mhra_ingredients.json", "w", encoding="utf-8") as f:
        json.dump(ingredient_export, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(dataset)} GTIN records and {len(ingredient_export)} ingredients.")


if __name__ == "__main__":
    build_gtin_json()
