import sqlite3
import json
import sys
import urllib.request
import re
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# 1. MHRA Yellow Card API Configuration & Static Fallback Dictionary
# ---------------------------------------------------------------------------
MHRA_YELLOW_CARD_URL = "https://products.mhra.gov.uk/api/idap/summary"

# rINN (NHS dm+d standard) <-> BAN / USAN Synonym Map for mapping resilience
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
    "co-codamol": ["paracetamol", "codeine phosphate"],
    "co-dydramol": ["paracetamol", "dihydrocodeine tartrate"]
}

# Static cardiac/arrhythmia risk fallback (guarantees offline risk flags if API is unavailable)
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
    "domperidone": ["QT Prolongation", "Arrhythmia", "Ventricular Arrhythmia"]
}

# Common salt forms and pharmacopeial suffixes to strip during normalization
SALTS_AND_SUFFIXES = r'\b(ph\.?\s*eur|usp|bp|hydrate|hydrochloride|hcl|maleate|sodium|sulfate|sulphate|besilate|mesilate|tartrate|trihydrate|dihydrate|phosphate|acetate|bromide)\b'

# ---------------------------------------------------------------------------
# 2. Text Normalization & Matching Logic
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """Strips salts, dosages, special characters, and normalizes spacing."""
    if not text:
        return ""
    clean = text.lower()
    clean = re.sub(SALTS_AND_SUFFIXES, '', clean)
    clean = re.sub(r'[^a-z0-9\s]', ' ', clean)
    return " ".join(clean.split())

def get_fuzzy_ratio(a: str, b: str) -> float:
    """Calculates Levenshtein similarity ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()

def find_mhra_match(dmd_ingredient: str, mhra_db: dict):
    """
    3-Tier Search:
      Tier 1: Direct Normalized Key Match
      Tier 2: Synonym Map Lookup (rINN <-> BAN)
      Tier 3: Fuzzy / Substring Token Match (>= 88% similarity)
    """
    norm_dmd = normalize_text(dmd_ingredient)
    if not norm_dmd:
        return None

    # Tier 1: Direct Match
    if norm_dmd in mhra_db:
        return mhra_db[norm_dmd]

    # Tier 2: Synonym Lookup
    if norm_dmd in DRUG_SYNONYMS:
        syn = DRUG_SYNONYMS[norm_dmd]
        if isinstance(syn, str) and syn in mhra_db:
            return mhra_db[syn]
        elif isinstance(syn, list):
            for item in syn:
                if item in mhra_db:
                    return mhra_db[item]

    # Tier 3: Substring & Fuzzy Match
    best_match = None
    highest_score = 0.0

    for mhra_key, mhra_data in mhra_db.items():
        if norm_dmd in mhra_key or mhra_key in norm_dmd:
            return mhra_data

        score = get_fuzzy_ratio(norm_dmd, mhra_key)
        if score > highest_score and score >= 0.88:
            highest_score = score
            best_match = mhra_data

    return best_match

# ---------------------------------------------------------------------------
# 3. Master MHRA Data Fetching & Dictionary Builder
# ---------------------------------------------------------------------------
def build_mhra_ingredient_database():
    """
    Fetches live MHRA Yellow Card iDAP summaries and indexes by ingredient key.
    Falls back gracefully to the static risk map if offline or blocked.
    """
    print("Building MHRA Master Ingredient Database...")
    ingredient_db = {}

    try:
        req = urllib.request.Request(
            MHRA_YELLOW_CARD_URL, 
            headers={'User-Agent': 'PharmaRiskScanner-CI-Pipeline/1.0'}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            raw_data = response.read().decode('utf-8')
            records = json.loads(raw_data)
            
            for item in records:
                raw_ing = item.get('substance_name', '')
                ing_key = normalize_text(raw_ing)
                reaction = item.get('reaction_term', '').strip()
                soc_category = item.get('soc_term', '').strip()

                if ing_key and reaction:
                    if ing_key not in ingredient_db:
                        ingredient_db[ing_key] = {
                            "ingredient_name": raw_ing.title(),
                            "has_cardiac_risk": False,
                            "cardiac_effects": set(),
                            "all_side_effects": set()
                        }
                    
                    ingredient_db[ing_key]["all_side_effects"].add(reaction)
                    
                    # Flag cardiac MedDRA categories or arrhythmia terms
                    is_cardiac_soc = "cardiac" in soc_category.lower()
                    is_arrhythmia_term = any(term in reaction.lower() for term in [
                        "arrhythmia", "tachycardia", "qt prolongation", 
                        "palpitations", "bradycardia", "cardiac arrest", "ventricular"
                    ])
                    
                    if is_cardiac_soc or is_arrhythmia_term:
                        ingredient_db[ing_key]["has_cardiac_risk"] = True
                        ingredient_db[ing_key]["cardiac_effects"].add(reaction)

        print(f"Loaded live MHRA data for {len(ingredient_db)} ingredients.")

    except Exception as e:
        print(f"Notice: Live MHRA endpoint fetch skipped ({e}). Applying static cardiac risk rules.")

    # Merge static fallback rules to ensure critical drug safety flags are never missed
    for fallback_ing, cardiac_terms in STATIC_CARDIAC_RISK_MAP.items():
        norm_key = normalize_text(fallback_ing)
        if norm_key not in ingredient_db:
            ingredient_db[norm_key] = {
                "ingredient_name": fallback_ing.title(),
                "has_cardiac_risk": True,
                "cardiac_effects": set(cardiac_terms),
                "all_side_effects": set(cardiac_terms)
            }
        else:
            ingredient_db[norm_key]["has_cardiac_risk"] = True
            ingredient_db[norm_key]["cardiac_effects"].update(cardiac_terms)

    return ingredient_db

# ---------------------------------------------------------------------------
# 4. SQLite Export Loop & JSON Packaging
# ---------------------------------------------------------------------------
def build_gtin_json():
    # 1. Generate MHRA Master Ingredient Dictionary
    mhra_ingredients = build_mhra_ingredient_database()

    try:
        conn = sqlite3.connect("dmd.db")
        cursor = conn.cursor()

        query = """
        SELECT 
            gtin.gtin AS gtin,
            ampp.nm AS product_name,
            GROUP_CONCAT(ing.nm, ' + ') AS raw_ingredients
        FROM gtin_ampp gtin
        JOIN ampp ON gtin.amppid = ampp.id
        LEFT JOIN vmp_ing ON ampp.vmpid = vmp_ing.vmpid
        LEFT JOIN ing ON vmp_ing.ingid = ing.id
        WHERE gtin.gtin IS NOT NULL
        GROUP BY gtin.gtin, ampp.nm;
        """

        cursor.execute(query)
        rows = cursor.fetchall()

        gtin_dataset = []

        for row in rows:
            gtin_str = str(row[0])
            prod_name = row[1] if row[1] else ""
            raw_ing_str = row[2] if row[2] else ""
            
            split_ingredients = [i.strip() for i in raw_ing_str.split('+') if i.strip()]
            
            combined_cardiac_effects = set()
            combined_side_effects = set()
            has_risk = False

            # Match active ingredients using 3-Tier Matcher
            for raw_ing in split_ingredients:
                match = find_mhra_match(raw_ing, mhra_ingredients)
                
                if match:
                    if match["has_cardiac_risk"]:
                        has_risk = True
                    combined_cardiac_effects.update(match["cardiac_effects"])
                    combined_side_effects.update(list(match["all_side_effects"])[:10])

            gtin_dataset.append({
                "gtin": gtin_str,
                "product_name": prod_name,
                "ingredients": raw_ing_str,
                "has_cardiac_risk": has_risk,
                "cardiac_effects": sorted(list(combined_cardiac_effects)),
                "side_effects_summary": sorted(list(combined_side_effects))
            })

        # Save primary GTIN database for Android Room / Barcode scanning
        with open("dmd_gtin_latest.json", "w", encoding="utf-8") as f:
            json.dump(gtin_dataset, f, indent=2, ensure_ascii=False)

        # Save standalone Master Ingredient database for OCR Camera lookups
        ingredient_export = {
            k: {
                "ingredient_name": v["ingredient_name"],
                "has_cardiac_risk": v["has_cardiac_risk"],
                "cardiac_effects": list(v["cardiac_effects"]),
                "side_effects": list(v["all_side_effects"])[:15]
            }
            for k, v in mhra_ingredients.items()
        }
        with open("mhra_ingredients.json", "w", encoding="utf-8") as f:
            json.dump(ingredient_export, f, indent=2, ensure_ascii=False)

        print(f"Successfully exported {len(gtin_dataset)} GTIN records and {len(ingredient_export)} MHRA ingredients.")

    except sqlite3.Error as e:
        print(f"SQLite Query Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    build_gtin_json()
