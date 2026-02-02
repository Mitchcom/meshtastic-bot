import sqlite3
import sys

try:
    conn = sqlite3.connect('/app/data/node_db.sqlite')
    cursor = conn.cursor()
    cursor.execute("SELECT long_name, short_name, id FROM nodes WHERE short_name LIKE '%mte4%' OR long_name LIKE '%mte4%'")
    rows = cursor.fetchall()
    if not rows:
        print("No node found with 'mte4' in name.")
    for row in rows:
        print(f"Found: {row}")
    conn.close()
except Exception as e:
    print(f"Error: {e}")
