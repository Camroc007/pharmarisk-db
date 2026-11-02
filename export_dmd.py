import json
import sqlite3
import sys

DB_PATH = "dmd.db"
OUT_PATH = "dmd_gtin_latest.json"

# NOTE: table/column names below are a best guess at the dmd SQLite schema.
# If the query fails, the script prints the real schema so it can be fixed.
QUERY = """
SELECT
    gtin.gtin AS gtin,
    ampp.nm AS product_name,
    GROUP_CONCAT(DISTINCT ing.nm) AS ingredients
FROM gtin_ampp gtin
JOIN ampp ON gtin.amppid = ampp.id
LEFT JOIN vmp_ing ON ampp.vmpid = vmp_ing.vmpid
LEFT JOIN ing ON vmp_ing.ingid = ing.id
WHERE gtin.gtin IS NOT NULL
GROUP BY gtin.gtin, ampp.nm;
"""


def dump_schema(conn):
    print("---- Actual database schema ----", file=sys.stderr)
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'view') ORDER BY name"
    ):
        print(f"-- {name}\n{sql}\n", file=sys.stderr)


def build_gtin_json():
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(QUERY).fetchall()
    except sqlite3.Error as e:
        print(f"SQLite query error: {e}", file=sys.stderr)
        dump_schema(conn)
        sys.exit(1)

    if not rows:
        print("Query returned no rows; not overwriting existing dataset.", file=sys.stderr)
        dump_schema(conn)
        sys.exit(1)

    dataset = [
        {
            "gtin": str(gtin),
            "product_name": name or "",
            "ingredients": ingredients or "",
        }
        for gtin, name, ingredients in rows
    ]

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)

    print(f"Successfully exported {len(dataset)} GTIN records.")


if __name__ == "__main__":
    build_gtin_json()
