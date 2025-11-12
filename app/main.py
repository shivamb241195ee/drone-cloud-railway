# app/main.py
import os
import sqlite3
import time
import requests
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from typing import List, Optional

# -----------------------
# Configuration (env vars)
# -----------------------
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "change-this-secret")
CLOUDINARY_UPLOAD_URL = os.getenv("CLOUDINARY_UPLOAD_URL")  # e.g. https://api.cloudinary.com/v1_1/<cloud_name>/image/upload
CLOUDINARY_UPLOAD_PRESET = os.getenv("CLOUDINARY_UPLOAD_PRESET")  # if using unsigned preset
DB_PATH = os.getenv("SQLITE_PATH", "/data/telemetry.db")

# Ensure data directory exists for sqlite on Railway
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

# -----------------------
# Database: simple sqlite
# -----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telemetry(
            time TEXT,
            lat REAL,
            lon REAL,
            alt REAL,
            batt REAL,
            extra TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# -----------------------
# App and static files
# -----------------------
app = FastAPI(title="Drone Cloud (Railway)")

# Allow your web UI (served from same origin) and other clients as needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # set explicit origin in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# serve static UI from app/static
app.mount("/static", StaticFiles(directory="static"), name="static")

# -----------------------
# WebSocket manager
# -----------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket, token: Optional[str]):
        # Basic token check
        if token != AUTH_TOKEN:
            await websocket.close(code=4001)
            return False
        await websocket.accept()
        self.active.append(websocket)
        return True

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, message: str, sender: Optional[WebSocket] = None):
        """Send message to all clients except the sender (if provided)."""
        for conn in list(self.active):
            try:
                if conn != sender:
                    await conn.send_text(message)
            except:
                self.disconnect(conn)

manager = ConnectionManager()

# WebSocket endpoint: clients must connect with token query param
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    ok = await manager.connect(websocket, token)
    if not ok:
        return
    try:
        while True:
            data = await websocket.receive_text()
            # If a connected client (e.g., dashboard) sends a command, broadcast to others (phone)
            # Commands are short strings (e.g., TAKEOFF, LAND, CAPTURE, SPEAK:Hello)
            await manager.broadcast(data, sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# -----------------------
# Telemetry endpoints
# -----------------------
@app.post("/api/telemetry")
async def telemetry(payload: dict):
    """
    Expected JSON:
    {"lat":11.11, "lon":75.75, "alt":10, "batt":88, "extra": "text optional"}
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO telemetry VALUES (?,?,?,?,?)",
            (datetime.utcnow().isoformat(),
             payload.get("lat"),
             payload.get("lon"),
             payload.get("alt"),
             payload.get("batt"))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return JSONResponse({"status":"error","detail":str(e)}, status_code=500)
    # optionally broadcast latest telemetry via WS to UI clients
    try:
        await manager.broadcast(json.dumps({"type":"telemetry","data":payload}))
    except:
        pass
    return {"status":"ok"}

@app.get("/api/telemetry/latest")
def latest():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM telemetry ORDER BY time DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {}
    return {"time": row[0], "lat": row[1], "lon": row[2], "alt": row[3], "batt": row[4]}

# -----------------------
# Photo upload endpoint
# -----------------------
@app.post("/api/photo")
async def upload_photo(file: UploadFile = File(...), token: str = Form(...), meta: Optional[str] = Form(None)):
    """
    Phone posts multipart/form-data:
    - file: the JPEG image
    - token: AUTH_TOKEN
    - meta: optional JSON or text
    Returns: {"status":"ok","url": "..."}
    """
    if token != AUTH_TOKEN:
        return JSONResponse({"status":"forbidden"}, status_code=403)

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
            return JSONResponse({"status":"error","detail":f"Cloud upload failed: {e}"}, status_code=500)
        if r.status_code not in (200,201):
            return JSONResponse({"status":"error","detail":r.text}, status_code=500)
        resp = r.json()
        url = resp.get("secure_url") or resp.get("url")
        # Broadcast photo url to connected clients via WS
        try:
            await manager.broadcast(f"PHOTO:{url}")
        except:
            pass
        return {"status":"ok", "url": url, "raw": resp}

    # Else store file locally (ephemeral on Railway) and serve via static if possible
    else:
        # write to /data and return route (note: Railway disk is ephemeral)
        fname = f"/data/photo_{int(time.time())}.jpg"
        with open(fname, "wb") as f:
            f.write(contents)
        # In this fallback we can't serve the file via static easily without additional config,
        # so just return success and the local file path (for dev only).
        try:
            await manager.broadcast(f"PHOTO:LOCAL:{fname}")
        except:
            pass
        return {"status":"ok", "url": fname}

# -----------------------
# Small root & health endpoints
# -----------------------
@app.get("/")
def root():
    return {"status":"ok","note":"Drone Cloud is running. See /static/index.html for UI."}

@app.get("/health")
def health():
    return {"status":"healthy","time": datetime.utcnow().isoformat()}

