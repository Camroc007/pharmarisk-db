import sqlite3
import json
import sys
import urllib.request
import re
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# 1. Configuration & Safety Fallbacks
# ---------------------------------------------------------------------------
MHRA_YELLOW_CARD_URL = "https://products.mhra.gov.uk/api/idap/summary"

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

SALTS_AND_SUFFIXES = r'\b(ph\.?\s*eur|usp|bp|hydrate|hydrochloride|hcl|maleate|sodium|sulfate|sulphate|besilate|mesilate|tartrate|trihydrate|dihydrate|phosphate|acetate|bromide)\b'

# ---------------------------------------------------------------------------
# 2. Text Normalization & Matching Logic
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    if not text:
        return ""
    clean = text.lower()
    clean = re.sub(SALTS_AND_SUFFIXES, '', clean)
    clean = re.sub(r'[^a-z0-9\s]', ' ', clean)
    return " ".join(clean.split())

def get_fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

def find_mhra_match(dmd_ingredient: str, mhra_db: dict):
    norm_dmd = normalize_text(dmd_ingredient)
    if not norm_dmd:
        return None

    if norm_dmd in mhra_db:
        return mhra_db[norm_dmd]

    if norm_dmd in DRUG_SYNONYMS:
        syn = DRUG_SYNONYMS[norm_dmd]
        if isinstance(syn, str) and syn in mhra_db:
            return mhra_db[syn]
        elif isinstance(syn, list):
            for item in syn:
                if item in mhra_db:
                    return mhra_db[item]

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
# 3. MHRA Master Ingredient Database Fetcher
# ---------------------------------------------------------------------------
def build_mhra_ingredient_database():
    print("Building MHRA Master Ingredient Database...")
    ingredient_db = {}

    try:
        req = urllib.request.Request(
            MHRA_YELLOW_CARD_URL, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
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
# 4. Introspective Schema Resolver & GTIN Exporter
# ---------------------------------------------------------------------------
def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name});")
    return [row[1] for row in cursor.fetchall()]

def find_column(columns, candidates):
    for cand in candidates:
        for col in columns:
            if col.upper() == cand.upper():
                return col
    return None

def build_gtin_json():
    mhra_ingredients = build_mhra_ingredient_database()

    try:
        conn = sqlite3.connect("dmd.db")
        cursor = conn.cursor()

        # 1. Inspect schema tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]

        # Determine GTIN table
        gtin_table = None
        for t in ["GTIN__AMPP", "gtin_ampp", "gtin", "GTIN"]:
            if t in tables:
                gtin_table = t
                break

        if not gtin_table:
            raise Exception("Could not locate GTIN table in dmd.db")

        # Inspect columns for GTIN and AMPP tables
        gtin_cols = get_table_columns(cursor, gtin_table)
        ampp_cols = get_table_columns(cursor, "AMPP")
        vmp_ing_cols = get_table_columns(cursor, "VMP__VIRTUAL_PRODUCT_INGREDIENT")
        ing_cols = get_table_columns(cursor, "INGREDIENT")

        # Resolve primary key and foreign key column names
        gtin_val_col = find_column(gtin_cols, ["GTIN", "gtin"])
        gtin_ampp_fk = find_column(gtin_cols, ["AMPPID", "APPID", "amppid", "id"])
        
        ampp_pk = find_column(ampp_cols, ["AMPPID", "APPID", "ID", "id"])
        ampp_name_col = find_column(ampp_cols, ["NM", "nm", "NAME", "name"])
        ampp_vmp_fk = find_column(ampp_cols, ["VMPID", "VPID", "vmpid"])

        vmp_ing_vmp_fk = find_column(vmp_ing_cols, ["VMPID", "VPID", "vmpid"])
        vmp_ing_ing_fk = find_column(vmp_ing_cols, ["INGID", "ISID", "ingid"])

        ing_pk = find_column(ing_cols, ["INGID", "ISID", "ID", "id"])
        ing_name_col = find_column(ing_cols, ["NM", "nm", "NAME", "name"])

        print(f"Resolved Schema Mapping: GTIN Table='{gtin_table}', GTIN Col='{gtin_val_col}', AMPP PK='{ampp_pk}', Ingredient Name Col='{ing_name_col}'")

        # Build dynamic query based on resolved columns
        query = f"""
        SELECT 
            gtin.{gtin_val_col} AS gtin,
            ampp.{ampp_name_col} AS product_name,
            GROUP_CONCAT(ing.{ing_name_col}, ' + ') AS raw_ingredients
        FROM {gtin_table} gtin
        JOIN AMPP ampp ON gtin.{gtin_ampp_fk} = ampp.{ampp_pk}
        LEFT JOIN VMP__VIRTUAL_PRODUCT_INGREDIENT vmp_ing ON ampp.{ampp_vmp_fk} = vmp_ing.{vmp_ing_vmp_fk}
        LEFT JOIN INGREDIENT ing ON vmp_ing.{vmp_ing_ing_fk} = ing.{ing_pk}
        WHERE gtin.{gtin_val_col} IS NOT NULL
        GROUP BY gtin.{gtin_val_col}, ampp.{ampp_name_col};
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

        with open("dmd_gtin_latest.json", "w", encoding="utf-8") as f:
            json.dump(gtin_dataset, f, indent=2, ensure_ascii=False)

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

        print(f"Successfully exported {len(gtin_dataset)} GTIN records and {len(ingredient_export)} ingredients.")

    except sqlite3.Error as e:
        print(f"SQLite Query Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Export Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    build_gtin_json()
