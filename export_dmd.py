import sqlite3
import json
import sys

def build_gtin_json():
    try:
        conn = sqlite3.connect("dmd.db")
        cursor = conn.cursor()

        # Query GTINs, Product Names (AMPP/VMP), and Active Ingredients (ing)
        query = """
        SELECT 
            gtin.gtin AS gtin,
            ampp.nm AS product_name,
            GROUP_CONCAT(ing.nm, ', ') AS ingredients
        FROM gtin_ampp gtin
        JOIN ampp ON gtin.amppid = ampp.id
        LEFT JOIN vmp_ing ON ampp.vmpid = vmp_ing.vmpid
        LEFT JOIN ing ON vmp_ing.ingid = ing.id
        WHERE gtin.gtin IS NOT NULL
        GROUP BY gtin.gtin, ampp.nm;
        """

        cursor.execute(query)
        rows = cursor.fetchall()

        dataset = []
        for row in rows:
            dataset.append({
                "gtin": str(row[0]),
                "product_name": row[1] if row[1] else "",
                "ingredients": row[2] if row[2] else ""
            })

        with open("dmd_gtin_latest.json", "w", encoding="utf-8") as f:
            json.dump(dataset, f, indent=2, ensure_ascii=False)

        print(f"Successfully exported {len(dataset)} GTIN records.")

    except sqlite3.Error as e:
        print(f"SQLite Query Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    build_gtin_json()
