import os, sqlite3
from fastapi import FastAPI

DB = os.environ.get("SQLITE_PATH", "/data/telemetry.db")
os.makedirs("/data", exist_ok=True)
conn = sqlite3.connect(DB)
conn.execute("CREATE TABLE IF NOT EXISTS telemetry(time TEXT, lat REAL, lon REAL, alt REAL, batt REAL)")
conn.commit()
conn.close()

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok", "note": "Deploy ready"}
