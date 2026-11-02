import json
import sqlite3
import sys

DB_PATH = "dmd.db"
OUT_PATH = "dmd_gtin_latest.json"

QUERY = """
WITH vmp_ingredients AS (
    SELECT VPID, GROUP_CONCAT(NM, ', ') AS ingredients
    FROM (
        SELECT DISTINCT vi.VPID AS VPID, i.NM AS NM
        FROM VMP__VIRTUAL_PRODUCT_INGREDIENT vi
        JOIN INGREDIENT i ON i.ISID = vi.ISID
        ORDER BY i.NM
    )
    GROUP BY VPID
)
SELECT
    g.GTIN AS gtin,
    ampp.NM AS product_name,
    vi.ingredients AS ingredients
FROM GTIN__AMPP g
JOIN AMPP ampp ON ampp.APPID = g.AMPPID
LEFT JOIN AMP amp ON amp.APID = ampp.APID
LEFT JOIN vmp_ingredients vi ON vi.VPID = amp.VPID
WHERE g.GTIN IS NOT NULL AND g.GTIN != ''
ORDER BY g.GTIN;
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
