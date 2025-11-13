# app/main.py
import os
import sqlite3
import time
import json
from datetime import datetime, timezone
from typing import List, Optional

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# -----------------------
# Configuration (env vars)
# -----------------------
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "change-this-secret")  # change in production
CLOUDINARY_UPLOAD_URL = os.getenv("CLOUDINARY_UPLOAD_URL")  # e.g. https://api.cloudinary.com/v1_1/<cloud>/image/upload
CLOUDINARY_UPLOAD_PRESET = os.getenv("CLOUDINARY_UPLOAD_PRESET")  # if using unsigned preset
DB_PATH = os.getenv("SQLITE_PATH", "/data/telemetry.db")
PHOTOS_DIR = os.getenv("PHOTOS_DIR", "/data/photos")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # e.g. https://your-app.up.railway.app

# Ensure directories exist
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
os.makedirs(PHOTOS_DIR, exist_ok=True)

# -----------------------
# SQLite helper
# -----------------------
def get_db_conn():
    # Using default sqlite3 connection; for production consider using a proper DB or an async DB driver.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry(
            time TEXT,
            lat REAL,
            lon REAL,
            alt REAL,
            batt REAL,
            meta TEXT
        )
        """
    )
    conn.commit()
    conn.close()

init_db()

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(title="Drone Cloud (Railway)")

# CORS: in production set explicit origins instead of "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static dashboard and photos
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/photos", StaticFiles(directory=PHOTOS_DIR), name="photos")

# -----------------------
# WebSocket connection manager
# -----------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket, token: Optional[str]):
        # Basic token check
        if token != AUTH_TOKEN:
            # close with custom code
            await websocket.close(code=4001)
            return False
        await websocket.accept()
        self.active.append(websocket)
        return True

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def send_personal(self, websocket: WebSocket, message: str):
        try:
            await websocket.send_text(message)
        except Exception:
            self.disconnect(websocket)

    async def broadcast(self, message: str, sender: Optional[WebSocket] = None):
        # iterate over a copy to allow modifications while iterating
        for conn in list(self.active):
            if conn == sender:
                continue
            try:
                await conn.send_text(message)
            except Exception:
                self.disconnect(conn)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    ok = await manager.connect(websocket, token)
    if not ok:
        return
    try:
        while True:
            data = await websocket.receive_text()
            # broadcast commands/acks to other clients
            await manager.broadcast(data, sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        # ensure connection removed on any unexpected error
        manager.disconnect(websocket)

# -----------------------
# Telemetry endpoints
# -----------------------
@app.post("/api/telemetry")
async def telemetry(payload: dict):
    """
    Expected JSON body:
    {"lat":11.11, "lon":75.75, "alt":10, "batt":88, "meta":"optional text or json string"}
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO telemetry VALUES (?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                payload.get("lat"),
                payload.get("lon"),
                payload.get("alt"),
                payload.get("batt"),
                payload.get("meta"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

    # broadcast telemetry via WS to UI clients (non-blocking best-effort)
    try:
        await manager.broadcast(json.dumps({"type": "telemetry", "data": payload}))
    except Exception:
        pass

    return {"status": "ok"}

@app.get("/api/telemetry/recent")
def telemetry_recent(limit: int = Query(50, ge=1, le=1000)):
    """
    Return recent telemetry rows, newest first.
    Query param: limit (default 50, max 1000)
    Returns: {rows: [{time, lat, lon, alt, batt, meta}, ...]}
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT time, lat, lon, alt, batt, meta FROM telemetry ORDER BY time DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

    out = [
        {"time": r[0], "lat": r[1], "lon": r[2], "alt": r[3], "batt": r[4], "meta": r[5]}
        for r in rows
    ]
    return {"rows": out}


@app.get("/api/telemetry/latest")
def latest():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM telemetry ORDER BY time DESC LIMIT 1").fetchone()
        conn.close()
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

    if not row:
        return {}
    return {"time": row[0], "lat": row[1], "lon": row[2], "alt": row[3], "batt": row[4], "meta": row[5]}

# -----------------------
# Photo upload (Cloudinary optional or local file store)
# -----------------------
@app.post("/api/photo")
async def upload_photo(request: Request, file: UploadFile = File(...), token: str = Form(...), meta: Optional[str] = Form(None)):
    """
    POST multipart/form-data:
      - file (image)
      - token (AUTH_TOKEN)
      - meta (optional)
    Response: {"status":"ok","url": "<public url to /photos/...>"}
    """
    if token != AUTH_TOKEN:
        return JSONResponse({"status": "forbidden"}, status_code=403)

    contents = await file.read()

    # If Cloudinary configured, upload there
    if CLOUDINARY_UPLOAD_URL and CLOUDINARY_UPLOAD_PRESET:
        files = {'file': (file.filename, contents)}
        data = {'upload_preset': CLOUDINARY_UPLOAD_PRESET}
        if meta:
            data['context'] = meta
        try:
            r = requests.post(CLOUDINARY_UPLOAD_URL, files=files, data=data, timeout=30)
        except Exception as e:
            return JSONResponse({"status": "error", "detail": f"Cloud upload failed: {e}"}, status_code=500)
        if r.status_code not in (200, 201):
            return JSONResponse({"status": "error", "detail": r.text}, status_code=500)
        resp = r.json()
        url = resp.get("secure_url") or resp.get("url")
        # Broadcast photo url to connected clients via WS
        try:
            await manager.broadcast(f"PHOTO:{url}")
        except Exception:
            pass
        return {"status": "ok", "url": url, "raw": resp}

    # Otherwise store file locally and return accessible URL
    ts = int(time.time() * 1000)
    safe_name = f"photo_{ts}_{file.filename}".replace(" ", "_")
    path = os.path.join(PHOTOS_DIR, safe_name)
    try:
        with open(path, "wb") as f:
            f.write(contents)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": f"write failed: {e}"}, status_code=500)

    # Build accessible URL. Prefer PUBLIC_URL env var if set, else use request.base_url
    if PUBLIC_URL:
        base = PUBLIC_URL.rstrip("/")
    else:
        # request.base_url includes trailing slash, convert to scheme+host (ends with '/')
        base = str(request.base_url).rstrip("/")

    photo_url = f"{base}/photos/{safe_name}"

    # broadcast to all WS clients
    try:
        await manager.broadcast(f"PHOTO:{photo_url}")
    except Exception:
        pass

    return {"status": "ok", "url": photo_url}

# -----------------------
# Root & health endpoints
# -----------------------
@app.get("/")
def root():
    return {"status": "ok", "note": "Drone Cloud running. Visit /static/index.html for UI."}

@app.get("/health")
def health():
    return {"status": "healthy", "time": datetime.now(timezone.utc).isoformat()}

# If you run this module directly (development)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)


