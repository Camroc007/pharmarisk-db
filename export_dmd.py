import sqlite3
import json

def build_gtin_json():
    # Connect to the SQLite database produced by wardle/dmd
    conn = sqlite3.connect("dmd.db")
    cursor = conn.cursor()

    # Query GTINs, Product Names, and Active Ingredients
    query = """
    SELECT 
        gtin.gtin AS gtin,
        ampp.nm AS product_name,
        GROUP_CONCAT(ing.nm, ', ') AS ingredients
    FROM ampp
    JOIN gtin ON ampp.id = gtin.amppid
    JOIN ampp_vmp_map ON ampp.vmpid = ampp_vmp_map.vmpid
    JOIN vmp_ingredient ing ON ampp_vmp_map.vmpid = ing.vmpid
    WHERE gtin.gtin IS NOT NULL
    GROUP BY gtin.gtin;
    """

    cursor.execute(query)
    rows = cursor.fetchall()

    dataset = []
    for row in rows:
        dataset.append({
            "gtin": str(row[0]),
            "product_name": row[1],
            "ingredients": row[2] if row[2] else ""
        })

    # Save to the JSON file expected by the Android app
    with open("dmd_gtin_latest.json", "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)

    print(f"Successfully exported {len(dataset)} GTIN records.")

if __name__ == "__main__":
    build_gtin_json()
