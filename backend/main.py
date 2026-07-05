"""
随心一阅 Cloud Site
Minimal static site server — serves landing page and download files.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn

# ─── Paths ─────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
APP_VERSION = "2.0.0"

# ─── App ───────────────────────────────────────────────────────────
app = FastAPI(
    title="随心一阅",
    version=APP_VERSION,
    docs_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Static Pages ──────────────────────────────────────────────────

@app.get("/")
def serve_landing():
    landing = BASE_DIR / "landing.html"
    if landing.exists():
        return FileResponse(str(landing), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return HTMLResponse("<h1>随心一阅</h1>", status_code=200)

@app.get("/app/console")
@app.get("/app/console/{subpath:path}")
def serve_admin(subpath: str = ""):
    return HTMLResponse("<h1>管理后台已迁移至本地版本</h1>", status_code=200)

@app.get("/downloads/{filename}")
def serve_download(filename: str):
    downloads = BASE_DIR / "downloads"
    path = downloads / filename
    if not path.resolve().is_relative_to(downloads.resolve()):
        raise HTTPException(404)
    if not path.is_file():
        raise HTTPException(404, f"File not found: {filename}")
    return FileResponse(str(path))

@app.get("/favicon.ico")
def serve_favicon():
    for name in ["favicon.ico", "favicon.svg", "app_icon.png"]:
        p = BASE_DIR / name
        if p.exists():
            return FileResponse(str(p))
    raise HTTPException(404)

@app.get("/favicon.svg")
def serve_favicon_svg():
    return serve_favicon()

@app.get("/app_icon.png")
def serve_app_icon():
    p = BASE_DIR / "app_icon.png"
    if p.exists():
        return FileResponse(str(p))
    raise HTTPException(404)

# ─── API ───────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "随心一阅", "version": APP_VERSION}

@app.get("/api/client/update")
def client_update(version: str = "", arch: str = "x86", platform: str = "win10"):
    """Update manifest endpoint. Returns newer version info or empty."""
    return {
        "latest_version": APP_VERSION,
        "has_update": False,
        "notes": [],
        "download_url": "",
        "sha256": "",
    }


# ─── Entry Point ───────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8765, reload=True)
