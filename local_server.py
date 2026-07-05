"""
随心一阅 Local Server
Local RPA grading server that runs on the user's machine.
Handles screen capture, mouse/keyboard automation, and proxies
auth API calls to the cloud server.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import secrets
import threading
import traceback
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


class JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


APP_VERSION = "2.0.0"
APP_NAME = "Suixin Yiyue"

# ─── Paths ─────────────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    _appdata = Path(os.environ.get('APPDATA', Path.home() / '.config'))
    BASE_DIR = _appdata / '随心一阅'
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    _MEIPASS = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent
    _MEIPASS = BASE_DIR

CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
UPDATE_DIR = BASE_DIR / "updates"
DIAGNOSTIC_DIR = BASE_DIR / "diagnostics"
LOCAL_SECURITY_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "SuixinYiyue"
LOCAL_TOKEN_PATH = LOCAL_SECURITY_DIR / "local_token.txt"
for _dir in (LOG_DIR, UPDATE_DIR, DIAGNOSTIC_DIR, LOCAL_SECURITY_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "client.log"
EVENTS_PATH = LOG_DIR / "client_events.jsonl"

# ─── Local Grading History DB ─────────────────────────────────────
import sqlite3 as _sqlite3

HISTORY_DB_PATH = BASE_DIR / "grading_history.db"

def _get_history_db() -> _sqlite3.Connection:
    conn = _sqlite3.connect(str(HISTORY_DB_PATH))
    conn.row_factory = _sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _init_history_db() -> None:
    with _get_history_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS grading_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_score REAL,
                max_total REAL,
                questions TEXT,
                summary TEXT,
                usage_json TEXT,
                cost REAL,
                model TEXT,
                feedback TEXT DEFAULT NULL,
                is_loop INTEGER DEFAULT 0,
                batch_id TEXT,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()

_init_history_db()

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("suixin.local")


def _record_event(kind: str, details: dict | None = None) -> None:
    payload = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "app_version": APP_VERSION,
        "details": details or {},
    }
    try:
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _record_error(context: str, exc: Exception | str, extra: dict | None = None) -> None:
    message = str(exc)
    logger.error("%s: %s", context, message, exc_info=not isinstance(exc, str))
    _record_event("error", {"context": context, "message": message, **(extra or {})})


def _install_exception_hooks() -> None:
    def _sys_hook(exc_type, exc, tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        _record_event("crash", {"message": str(exc)})

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            logger.critical(
                "Unhandled thread exception: %s",
                getattr(args.thread, "name", "thread"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            _record_event("thread_crash", {"thread": getattr(args.thread, "name", ""), "message": str(args.exc_value)})
        threading.excepthook = _thread_hook


_install_exception_hooks()

# ─── Frontend SPA location ─────────────────────────────────────────
FRONTEND_DIR = None
for _d in [
    _MEIPASS / "frontend-dist",
    _MEIPASS.parent / "frontend-dist",
    BASE_DIR / "frontend-dist",
    BASE_DIR.parent / "frontend-dist",
    BASE_DIR.parent / "frontend" / "dist",
]:
    if _d.exists() and (_d / "index.html").exists():
        FRONTEND_DIR = _d
        break

# ─── Local Security Token ──────────────────────────────────────────
# Generated once and stored outside the user-editable config file. Browsers receive
# it as an HttpOnly localhost cookie so page scripts cannot read the RPA token.
def _init_local_token() -> str:
    if LOCAL_TOKEN_PATH.exists():
        try:
            token = LOCAL_TOKEN_PATH.read_text("utf-8").strip()
            if token:
                return token
        except OSError:
            pass

    token = ""
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
            token = str(cfg.pop("local_token", "") or "")
            if "local_token" not in cfg:
                CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        except (json.JSONDecodeError, OSError):
            token = ""

    token = token or secrets.token_urlsafe(32)
    LOCAL_TOKEN_PATH.write_text(token, "utf-8")
    try:
        os.chmod(LOCAL_TOKEN_PATH, 0o600)
    except OSError:
        pass
    return token

LOCAL_TOKEN = _init_local_token()

# ─── Local paths that DO need local token ──────────────────────────
_LOCAL_API_PATHS = {
    "/api/config", "/api/config/grading", "/api/select-region",
    "/api/grade/status", "/api/grade", "/api/grade/trial",
    "/api/grade/start-loop", "/api/grade/stop", "/api/grade/pause",
    "/api/estimate-cost", "/api/analyze-questions",
    "/api/update/check", "/api/update/download",
    "/api/diagnostics/export", "/api/diagnostics/report-error",
    "/api/grading/results",
}

# ─── App ───────────────────────────────────────────────────────────
app = FastAPI(
    title="随心一阅 Local",
    version=APP_VERSION,
    docs_url=None,       # disable /docs
    openapi_url=None,    # disable /openapi.json
    default_response_class=JSONResponse,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8766", "http://localhost:8766"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── RPA modules (from PyInstaller bundle or source) ──────────────
if getattr(sys, 'frozen', False):
    sys.path.insert(0, str(_MEIPASS))
    sys.path.insert(0, str(_MEIPASS / "backend"))
else:
    _backend = BASE_DIR / "backend"
    if str(_backend) not in sys.path:
        sys.path.insert(0, str(_backend))


def _get_rpa_modules():
    """Lazy import RPA modules (may fail if dependencies missing)."""
    try:
        from grader import GraderEngine, GraderConfig
        from question_analyzer import QuestionAnalyzer, AnalysisConfig
        from screen_selector import ScreenSelector
        return GraderEngine, GraderConfig, QuestionAnalyzer, AnalysisConfig, ScreenSelector
    except ImportError as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(503, f"RPA模块不可用: {e}")


def _get_local_api_creds() -> dict:
    """Read API credentials from local config file.
    Returns dict with keys: api_key, base_url, model. Raises HTTPException on failure."""
    if not CONFIG_PATH.exists():
        raise HTTPException(400, "请先在设置中配置 API Key")
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        raise HTTPException(400, "配置文件读取失败")
    api_cfg = cfg.get("api") or cfg.get("doubao") or {}
    api_key = api_cfg.get("api_key", "")
    if not api_key:
        raise HTTPException(400, "请先在设置中配置 API Key")
    base_url = api_cfg.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
    model = api_cfg.get("model", "doubao-seed-2-0-mini-260428")
    return {"api_key": api_key, "base_url": base_url, "model": model}


# ─── Local Token Middleware ────────────────────────────────────────
@app.middleware("http")
async def local_token_middleware(request: Request, call_next):
    path = request.url.path

    # Only check /api/* routes
    if not path.startswith("/api/"):
        return await call_next(request)

    # Health is always public
    if path in ("/api/health", "/api/shutdown"):
        return await call_next(request)

    # Local API — require X-Local-Token
    token = request.headers.get("X-Local-Token") or request.cookies.get("sy_local_token") or ""
    if not token or not secrets.compare_digest(token, LOCAL_TOKEN):
        return JSONResponse(status_code=403, content={"detail": "禁止访问：缺少有效的本地令牌"})

    return await call_next(request)


# ─── Health / Info ─────────────────────────────────────────────────
def _windows_arch() -> str:
    return "x64" if struct.calcsize("P") * 8 == 64 else "x86"


def _windows_version_label() -> str:
    if os.name != "nt":
        return "windows"
    try:
        version = platform.version()
        build = int(version.split(".")[-1]) if version else 0
    except (ValueError, IndexError):
        build = 0
    release = platform.release()
    if build >= 22000:
        return "win11"
    if release == "10" or build >= 10000:
        return "win10"
    return "win8"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for item in str(value or "0").replace("-", ".").split("."):
        digits = "".join(ch for ch in item if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts or [0])


def _redact_local_config(cfg: dict) -> dict:
    safe = json.loads(json.dumps(cfg or {}, ensure_ascii=False))
    safe.pop("local_token", None)
    for key in ("api", "doubao"):
        if isinstance(safe.get(key), dict):
            safe[key].pop("api_key", None)
    return safe




@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "随心一阅 Local",
        "app_version": APP_VERSION,
        "arch": _windows_arch(),
        "platform": _windows_version_label(),
    }


@app.post("/api/shutdown")
def shutdown():
    """Shut down the local server gracefully."""
    def _do_shutdown():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"status": "shutting_down"}


GITHUB_RELEASES_URL = "https://api.github.com/repos/ronghua666/suixinyiyue/releases/latest"

def _fetch_update_manifest() -> dict:
    """Check GitHub Releases for updates (placeholder)."""
    return {
        "latest_version": APP_VERSION,
        "has_update": False,
        "notes": ["Update check via GitHub Releases coming soon."],
        "download_url": "",
        "sha256": "",
        "current_version": APP_VERSION,
        "arch": _windows_arch(),
    }


@app.get("/api/update/check")
def check_update():
    try:
        data = _fetch_update_manifest()
        latest = data.get("latest_version", APP_VERSION)
        data["has_update"] = _version_tuple(latest) > _version_tuple(APP_VERSION)
        return data
    except Exception as exc:
        _record_error("update_check", exc)
        return {
            "success": False,
            "has_update": False,
            "current_version": APP_VERSION,
            "arch": _windows_arch(),
            "error": f"检查更新失败：{exc}",
        }


@app.post("/api/update/download")
def download_update(data: dict | None = None):
    data = data or {}
    manifest = _fetch_update_manifest()
    if not manifest.get("has_update") and not data.get("force"):
        return {"success": True, "has_update": False, "message": "当前已是最新版本"}

    download_url = data.get("download_url") or manifest.get("download_url")
    if not download_url:
        raise HTTPException(400, "没有可下载的更新包")

    parsed = urlparse(download_url)
    filename = Path(parsed.path).name or f"SuixinYiyue-{manifest.get('latest_version', 'latest')}-{_windows_arch()}-setup.exe"
    if not filename.lower().endswith(".exe"):
        raise HTTPException(400, "更新包格式不正确")

    dest = UPDATE_DIR / filename
    tmp = dest.with_suffix(dest.suffix + ".download")
    try:
        with httpx.stream("GET", download_url, timeout=120.0, follow_redirects=True) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes():
                    if chunk:
                        f.write(chunk)
        tmp.replace(dest)
        expected_sha = manifest.get("sha256") or ""
        if expected_sha:
            import hashlib
            sha = hashlib.sha256(dest.read_bytes()).hexdigest()
            if sha.lower() != expected_sha.lower():
                dest.unlink(missing_ok=True)
                raise HTTPException(400, "更新包校验失败，请稍后重试")

        _record_event("update_downloaded", {"version": manifest.get("latest_version"), "path": str(dest)})
        if hasattr(os, "startfile"):
            os.startfile(str(dest))  # type: ignore[attr-defined]
        else:
            subprocess.Popen([str(dest)], close_fds=True)
        return {
            "success": True,
            "has_update": True,
            "latest_version": manifest.get("latest_version"),
            "installer_path": str(dest),
            "launched": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        _record_error("update_download", exc, {"url": download_url})
        raise HTTPException(500, f"下载更新失败：{exc}")


def _tail_text(path: Path, max_bytes: int = 300_000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def _build_diagnostic_zip(reason: str = "manual", description: str = "") -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    zip_path = DIAGNOSTIC_DIR / f"diagnostic-{ts}.zip"
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        except Exception:
            cfg = {"config_read_error": True}

    info = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "reason": reason,
        "description": description,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "windows": platform.platform(),
        "machine": platform.machine(),
        "arch": _windows_arch(),
        "python": sys.version,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "base_dir": str(BASE_DIR),
        "frontend_dir": str(FRONTEND_DIR) if FRONTEND_DIR else "",
    }

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.json", json.dumps(info, ensure_ascii=False, indent=2))
        z.writestr("config.redacted.json", json.dumps(_redact_local_config(cfg), ensure_ascii=False, indent=2))
        z.writestr("client.log", _tail_text(LOG_PATH))
        z.writestr("client_events.jsonl", _tail_text(EVENTS_PATH))
    return zip_path


@app.post("/api/diagnostics/export")
def export_diagnostics(data: dict | None = None):
    data = data or {}
    zip_path = _build_diagnostic_zip("manual_export", str(data.get("description", ""))[:1000])
    return FileResponse(str(zip_path), media_type="application/zip", filename=zip_path.name)



@app.post("/api/diagnostics/report-error")
def report_client_error(request: Request, data: dict | None = None):
    data = data or {}
    _record_error(
        "frontend_error",
        str(data.get("message", "未知错误"))[:1000],
        {
            "path": str(data.get("path", ""))[:200],
            "status": data.get("status"),
            "page": str(data.get("page", ""))[:100],
        },
    )
    auto_upload = bool(data.get("auto_upload"))
    if auto_upload:
        _record_event("diagnostic_auto_triggered", {"message": str(data.get("message", ""))[:1000]})
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
#  Config API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/config")
def get_config():
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        cfg.pop("local_token", None)
        return cfg
    return {"api": {}, "coords": {}}


@app.post("/api/config")
def save_config(data: dict):
    existing = {}
    if CONFIG_PATH.exists():
        existing = json.loads(CONFIG_PATH.read_text("utf-8"))
    existing.pop("local_token", None)
    existing.update(data)
    CONFIG_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), "utf-8")
    return {"status": "ok"}


@app.post("/api/config/grading")
def save_grading_config(data: dict):
    existing = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
    existing.pop("local_token", None)
    if isinstance(data, dict) and data.get("questions"):
        data["questions"] = _normalize_questions(data.get("questions", []))
    existing["grading"] = data
    CONFIG_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), "utf-8")
    return {"status": "ok"}


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_questions(questions: list[dict]) -> list[dict]:
    normalized = []
    for i, q in enumerate(questions or []):
        item = dict(q or {})
        item["number"] = str(item.get("number") or f"第{i + 1}空")
        item["description"] = str(item.get("description") or "")
        item["max_score"] = round(_to_float(item.get("max_score"), 0), 2)
        item["standard_answer"] = str(item.get("standard_answer") or "")
        item["grading_rubric"] = str(item.get("grading_rubric") or "")
        normalized.append(item)
    return normalized


def _validate_grading_ready(grading: dict) -> list[dict]:
    questions = _normalize_questions(grading.get("questions", []))
    if questions:
        for q in questions:
            if q["max_score"] <= 0:
                raise HTTPException(400, f"{q['number']} 的满分必须大于 0")
            if not q["grading_rubric"].strip():
                raise HTTPException(400, f"{q['number']} 的评分细则必填")
        return questions

    if not str(grading.get("grading_rubric", "")).strip():
        raise HTTPException(400, "请先填写评分细则（必填）")
    return []


# ═══════════════════════════════════════════════════════════════════
#  Screen Selection
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/select-region")
def select_region(req: dict):
    GraderEngine, GraderConfig, QuestionAnalyzer, AnalysisConfig, ScreenSelector = _get_rpa_modules()
    region_type = req.get("type", "answer")
    selector = ScreenSelector()
    rect = selector.select(region_type)
    if rect is None:
        return {"success": False, "error": "用户取消了选择"}
    coords = {"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]}
    existing = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
    existing.pop("local_token", None)
    if "coords" not in existing:
        existing["coords"] = {}
    existing["coords"][f"{region_type}_region"] = coords
    CONFIG_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), "utf-8")
    return {"success": True, "region": coords}


# ═══════════════════════════════════════════════════════════════════
#  Grading API
# ═══════════════════════════════════════════════════════════════════

_loop_state: dict = {"status": "idle", "error": None, "current": 0, "total": 0, "paused": False, "last_result": None}
_loop_lock = threading.Lock()


@app.get("/api/grade/status")
def get_grade_status():
    with _loop_lock:
        return {
            "status": _loop_state["status"],
            "error": _loop_state.get("error"),
            "current": _loop_state.get("current", 0),
            "total": _loop_state.get("total", 0),
            "paused": _loop_state.get("paused", False),
            "last_result": _loop_state.get("last_result"),
        }


@app.post("/api/analyze-questions")
def analyze_questions(request: Request):
    """Analyze answer region to detect individual blanks. Uses local API key."""
    creds = _get_local_api_creds()

    GraderEngine, GraderConfig, QuestionAnalyzer, AnalysisConfig, ScreenSelector = _get_rpa_modules()

    config = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
    coords = config.get("coords", {})

    if not coords.get("answer_region"):
        raise HTTPException(400, "请先框选【答题识别区】")

    region = (
        coords["answer_region"]["x"], coords["answer_region"]["y"],
        coords["answer_region"]["w"], coords["answer_region"]["h"],
    )

    analyzer = QuestionAnalyzer(
        api_key=creds["api_key"],
        base_url=creds["base_url"],
        model=creds["model"],
    )

    try:
        questions = analyzer.analyze(region)
    except Exception as e:
        traceback.print_exc()
        _record_error("analyze_questions", e)
        raise HTTPException(500, f"题目分析失败: {e}")

    # Save questions to config
    config.pop("local_token", None)
    config["grading"] = config.get("grading", {})
    config["grading"]["questions"] = questions
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")

    return {"questions": questions}


@app.post("/api/grade/trial")
def trial_grade(request: Request):
    """Trial grading: grade one paper and return timing/cost estimates for batch.
    This does a REAL grading run (screenshot → API → input score → submit).
    The elapsed time is used to estimate total time for N papers."""
    GraderEngine, GraderConfig, QuestionAnalyzer, AnalysisConfig, ScreenSelector = _get_rpa_modules()

    creds = _get_local_api_creds()

    config = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
    grading = config.get("grading", {})
    coords = config.get("coords", {})

    if not coords:
        raise HTTPException(400, "请先框选改卷区域")

    questions = _validate_grading_ready(grading)

    grader_config = GraderConfig(
        answer_region=coords.get("answer_region", {}),
        score_region=coords.get("score_region", {}),
        submit_region=coords.get("submit_region", {}),
        api_key=creds["api_key"],
        base_url=creds["base_url"],
        model=creds["model"],
        standard_answer=grading.get("standard_answer", ""),
        grading_rubric=grading.get("grading_rubric", ""),
        questions=questions,
    )

    engine = GraderEngine.from_config(grader_config)
    t0 = time.time()
    try:
        result = engine.run()
    except Exception as e:
        _record_error("trial_grade", e)
        return {
            "success": False,
            "total_score": 0,
            "max_total": 0,
            "reason": "",
            "error": str(e),
            "elapsed_seconds": time.time() - t0,
        }
    elapsed = time.time() - t0

    # Extract cost from usage
    usage = result.get("usage", {})
    cost_info = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }

    return {
        "success": True,
        "total_score": result.get("total_score", 0),
        "max_total": result.get("max_total", 0),
        "reason": result.get("reason", ""),
        "questions": result.get("questions", []),
        "blank_suspected": result.get("blank_suspected", False),
        "elapsed_seconds": round(elapsed, 1),
        "usage": cost_info,
    }


@app.post("/api/grade")
def start_grading(request: Request):
    """Start grading - single paper. Uses local API key."""
    GraderEngine, GraderConfig, QuestionAnalyzer, AnalysisConfig, ScreenSelector = _get_rpa_modules()

    creds = _get_local_api_creds()

    config = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
    grading = config.get("grading", {})
    coords = config.get("coords", {})

    if not coords:
        raise HTTPException(400, "请先框选改卷区域")

    questions = _validate_grading_ready(grading)

    grader_config = GraderConfig(
        answer_region=coords.get("answer_region", {}),
        score_region=coords.get("score_region", {}),
        submit_region=coords.get("submit_region", {}),
        api_key=creds["api_key"],
        base_url=creds["base_url"],
        model=creds["model"],
        standard_answer=grading.get("standard_answer", ""),
        grading_rubric=grading.get("grading_rubric", ""),
        questions=questions,
    )

    engine = GraderEngine.from_config(grader_config)
    try:
        result = engine.run()
        return {
            "success": True,
            "total_score": result.get("total_score", 0),
            "max_total": result.get("max_total", 0),
            "reason": result.get("reason", ""),
            "error": result.get("error"),
            "blank_suspected": result.get("blank_suspected", False),
        }
    except Exception as e:
        _record_error("single_grade", e)
        return {
            "success": False,
            "total_score": 0,
            "max_total": 0,
            "reason": "",
            "error": str(e),
        }


@app.post("/api/grade/start-loop")
def start_grade_loop(request: Request, data: dict = None):
    """Start continuous grading loop. Uses local API key.
    Accepts optional JSON body: { total_papers: int }."""
    global _loop_state
    if _loop_state["status"] == "running":
        raise HTTPException(400, "批改已经在运行中")

    data = data or {}
    total_papers = int(data.get("total_papers", 0)) or 0

    creds = _get_local_api_creds()

    GraderEngine, GraderConfig, QuestionAnalyzer, AnalysisConfig, ScreenSelector = _get_rpa_modules()

    config = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
    grading = config.get("grading", {})
    coords = config.get("coords", {})

    if not coords:
        raise HTTPException(400, "请先框选改卷区域")

    questions = _validate_grading_ready(grading)

    grader_config = GraderConfig(
        answer_region=coords.get("answer_region", {}),
        score_region=coords.get("score_region", {}),
        submit_region=coords.get("submit_region", {}),
        api_key=creds["api_key"],
        base_url=creds["base_url"],
        model=creds["model"],
        standard_answer=grading.get("standard_answer", ""),
        grading_rubric=grading.get("grading_rubric", ""),
        questions=questions,
    )

    with _loop_lock:
        _loop_state["status"] = "running"
        _loop_state["error"] = None
        _loop_state["current"] = 0
        _loop_state["total"] = total_papers
        _loop_state["paused"] = False

    from grader import _minimize_app_window, _restore_window, _keep_minimized, wait_for_next_paper

    def _run_loop():
        engine = GraderEngine.from_config(grader_config)
        app_win = _minimize_app_window()
        time.sleep(0.3)
        paper = 0
        try:
            while True:
                with _loop_lock:
                    if _loop_state["status"] != "running":
                        break
                    # Check pause — restore window so user can interact
                    while _loop_state.get("paused") and _loop_state["status"] == "running":
                        _restore_window(app_win)
                        _loop_lock.release()
                        time.sleep(0.5)
                        _loop_lock.acquire()
                        app_win = _minimize_app_window()
                        time.sleep(0.3)
                    if _loop_state["status"] != "running":
                        break
                    total = _loop_state.get("total", 0)
                paper += 1
                try:
                    prev_hash = engine.capture_hash()
                except Exception as e:
                    prev_hash = None
                    logger.warning("Could not capture paper hash before grading paper %s: %s", paper, e)

                result = engine.run_one()
                # Re-minimize the app window if submit/refresh restored it.
                # _keep_minimized only touches the saved window — never the exam page.
                app_win = _keep_minimized(app_win)
                time.sleep(0.1)
                with _loop_lock:
                    _loop_state["current"] = paper
                    _loop_state["last_result"] = {
                        "total_score": result.get("total_score", 0),
                        "max_total": result.get("max_total", 0),
                        "reason": result.get("reason", ""),
                        "questions": result.get("questions", []),
                        "blank_suspected": result.get("blank_suspected", False),
                        "is_loop": True,
                    }
                if result.get("error"):
                    with _loop_lock:
                        _loop_state["error"] = result["error"]
                        _loop_state["status"] = "error"
                    break
                if result.get("total_score", 0) < 0:
                    with _loop_lock:
                        _loop_state["status"] = "error"
                        _loop_state["error"] = "批改出现严重错误，已自动停止"
                    break
                # Stop when total reached
                if total > 0 and paper >= total:
                    with _loop_lock:
                        _loop_state["status"] = "idle"
                    break
                logger.info("Waiting for next paper after paper %s", paper)
                if prev_hash is None:
                    time.sleep(3.0)
                elif not wait_for_next_paper(engine, prev_hash):
                    with _loop_lock:
                        _loop_state["status"] = "error"
                        _loop_state["error"] = "等待下一份试卷超时，已停止，避免误判空白卷为0分"
                    break
        except Exception as e:
            _record_error("grade_loop", e, {"paper": paper})
            with _loop_lock:
                _loop_state["error"] = str(e)
                _loop_state["status"] = "error"
        finally:
            _restore_window(app_win)
            with _loop_lock:
                if _loop_state["status"] == "running":
                    _loop_state["status"] = "idle"
                elif _loop_state["status"] == "stopping":
                    _loop_state["status"] = "idle"

    threading.Thread(target=_run_loop, daemon=True).start()
    return {"status": "started"}


@app.post("/api/grade/stop")
def stop_grading():
    global _loop_state
    with _loop_lock:
        _loop_state["status"] = "stopping"
    return {"status": "stopping"}


@app.post("/api/grade/pause")
def pause_grading():
    """Toggle pause/resume for the grading loop."""
    global _loop_state
    with _loop_lock:
        if _loop_state["status"] != "running":
            raise HTTPException(400, "批改未在运行中")
        _loop_state["paused"] = not _loop_state.get("paused", False)
        return {"paused": _loop_state["paused"]}


# ═══════════════════════════════════════════════════════════════════
#  Cost Estimation
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/estimate-cost")
def estimate_cost(request: Request, data: dict):
    """Estimate the API cost for grading a paper by making a lightweight vision API call."""
    import base64
    from token_pricing import calculate_cost

    creds = _get_local_api_creds()

    GraderEngine, GraderConfig, QuestionAnalyzer, AnalysisConfig, ScreenSelector = _get_rpa_modules()

    config = json.loads(CONFIG_PATH.read_text("utf-8")) if CONFIG_PATH.exists() else {}
    coords = config.get("coords", {})

    if not coords.get("answer_region"):
        raise HTTPException(400, "请先框选【答题识别区】")

    engine = GraderEngine(
        api_key=creds["api_key"],
        base_url=creds["base_url"],
        model=creds["model"],
        standard_answer=data.get("standard_answer", "估算模式"),
        grading_rubric=data.get("grading_rubric", ""),
        answer_region=(
            coords["answer_region"]["x"], coords["answer_region"]["y"],
            coords["answer_region"]["w"], coords["answer_region"]["h"],
        ),
        score_region=(0, 0),
        submit_region=(0, 0),
    )

    engine._activate_exam_page()
    img_buf, img = engine._capture_answer_region()
    img_base64 = base64.b64encode(img_buf.read()).decode("utf-8")

    if engine._is_blank(img):
        return {"blank": True, "message": "检测到空白试卷", "cost_yuan": 0}

    grading_content = data.get("standard_answer", "")
    if data.get("grading_rubric"):
        grading_content += "\n\n【评分细则】\n" + data["grading_rubric"]

    _, usage = engine._call_vision_api(img_base64, grading_content)

    cost_info = calculate_cost(
        creds["model"],
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        usage.get("input_tokens_details", {}).get("cached_tokens", 0),
    )
    # Return only user-facing price, hide internal cost breakdown
    return {
        "blank": False,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
        "cost_yuan": cost_info["cost_yuan"],
    }


# ═══════════════════════════════════════════════════════════════════
#  Local Grading History
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/grading/results")
def save_grading_result(data: dict):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _get_history_db() as conn:
        cur = conn.execute(
            "INSERT INTO grading_results "
            "(total_score, max_total, questions, summary, usage_json, cost, model, is_loop, batch_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                data.get("total_score"),
                data.get("max_total"),
                json.dumps(data.get("questions", []), ensure_ascii=False),
                data.get("summary", ""),
                json.dumps(data.get("usage", {}), ensure_ascii=False),
                data.get("cost"),
                data.get("model", ""),
                data.get("is_loop", 0),
                data.get("batch_id"),
                now,
            ),
        )
        conn.commit()
        return {"success": True, "id": cur.lastrowid}


@app.get("/api/grading/results")
def get_grading_results(limit: int = 100, offset: int = 0, date_from: str = "", batch_id: str = ""):
    with _get_history_db() as conn:
        clauses = ["1=1"]
        params: list = []
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if date_from:
            clauses.append("created_at >= ?")
            params.append(date_from)
        where = " AND ".join(clauses)

        total = conn.execute(f"SELECT COUNT(*) FROM grading_results WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM grading_results WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["questions"] = json.loads(d.get("questions", "[]"))
            d["usage"] = json.loads(d.get("usage_json", "{}"))
            results.append(d)
        return {"success": True, "results": results, "total": total, "limit": limit, "offset": offset}


@app.get("/api/grading/results/summary")
def grading_summary(batch_id: str = ""):
    with _get_history_db() as conn:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM grading_results WHERE batch_id=? ORDER BY created_at ASC",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM grading_results ORDER BY created_at DESC LIMIT 200",
            ).fetchall()
            rows = list(reversed(rows))

        if not rows:
            return {"count": 0, "has_data": False}

        scores = []
        max_totals = []
        per_question: dict[str, list[float]] = {}
        total_cost = 0.0
        likes = 0
        dislikes = 0

        for r in rows:
            if r["total_score"] is not None:
                scores.append(r["total_score"])
            if r["max_total"] is not None:
                max_totals.append(r["max_total"])
            if r["cost"] is not None:
                total_cost += r["cost"]
            if r["feedback"] == "like":
                likes += 1
            elif r["feedback"] == "dislike":
                dislikes += 1

            questions = json.loads(r["questions"] or "[]")
            for q in questions:
                qnum = q.get("number", "?")
                qscore = q.get("score")
                qmax = q.get("max_score", 10)
                if qscore is not None and qmax > 0:
                    if qnum not in per_question:
                        per_question[qnum] = []
                    per_question[qnum].append(qscore / qmax)

        avg_score = round(sum(scores) / len(scores), 1) if scores else 0
        avg_max = round(sum(max_totals) / len(max_totals), 1) if max_totals else 0

        score_distribution = {"0-59": 0, "60-69": 0, "70-79": 0, "80-89": 0, "90-100": 0}
        for s in scores:
            pct = s / max_totals[0] * 100 if (max_totals and max_totals[0] > 0) else s
            if pct < 60:
                score_distribution["0-59"] += 1
            elif pct < 70:
                score_distribution["60-69"] += 1
            elif pct < 80:
                score_distribution["70-79"] += 1
            elif pct < 90:
                score_distribution["80-89"] += 1
            else:
                score_distribution["90-100"] += 1

        question_stats = {}
        for qnum, rates in per_question.items():
            if rates:
                question_stats[qnum] = {
                    "avg_rate": round(sum(rates) / len(rates) * 100, 1),
                    "count": len(rates),
                }

        return {
            "count": len(rows),
            "has_data": True,
            "avg_score": avg_score,
            "avg_max": avg_max,
            "avg_rate": round(avg_score / avg_max * 100, 1) if avg_max > 0 else 0,
            "score_distribution": score_distribution,
            "question_stats": question_stats,
            "total_cost": round(total_cost, 4),
            "likes": likes,
            "dislikes": dislikes,
        }


@app.get("/api/grading/results/export")
def export_grading_results_csv(date_from: str = "", batch_id: str = ""):
    import csv as _csv
    import io as _io

    with _get_history_db() as conn:
        clauses = ["1=1"]
        params: list = []
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if date_from:
            clauses.append("created_at >= ?")
            params.append(date_from)
        where = " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT * FROM grading_results WHERE {where} ORDER BY created_at DESC LIMIT 10000",
            params,
        ).fetchall()

    output = _io.StringIO()
    writer = _csv.writer(output)
    writer.writerow(["ID", "总分", "满分", "题目详情", "评语", "费用", "模型", "反馈", "时间"])
    for r in rows:
        questions = json.loads(r["questions"] or "[]")
        questions_str = "; ".join(
            f"{q.get('number','')}:{q.get('score','-')}/{q.get('max_score','-')}"
            for q in questions
        ) if questions else ""
        writer.writerow([
            r["id"], r["total_score"], r["max_total"],
            questions_str, r["summary"] or "", r["cost"] or "",
            r["model"] or "", r["feedback"] or "", r["created_at"] or "",
        ])

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=grading_results.csv"},
    )


@app.get("/api/grading/results/{result_id}")
def get_grading_result_detail(result_id: int):
    with _get_history_db() as conn:
        row = conn.execute("SELECT * FROM grading_results WHERE id=?", (result_id,)).fetchone()
        if not row:
            raise HTTPException(404, "记录不存在")
        d = dict(row)
        d["questions"] = json.loads(d.get("questions", "[]"))
        d["usage"] = json.loads(d.get("usage_json", "{}"))
        return {"success": True, "result": d}


@app.post("/api/grading/results/{result_id}/feedback")
async def submit_feedback(request: Request, result_id: int):
    try:
        body = await request.json()
    except Exception:
        body = {}
    fb = body.get("feedback", "")
    if fb not in ("like", "dislike"):
        raise HTTPException(400, "反馈类型无效")
    with _get_history_db() as conn:
        row = conn.execute("SELECT id FROM grading_results WHERE id=?", (result_id,)).fetchone()
        if not row:
            raise HTTPException(404, "记录不存在")
        conn.execute("UPDATE grading_results SET feedback=? WHERE id=?", (fb, result_id))
        conn.commit()
        return {"success": True}


# ═══════════════════════════════════════════════════════════════════
#  Frontend SPA (with local token injection)
# ═══════════════════════════════════════════════════════════════════

_FE_DIR = FRONTEND_DIR
for _d in [FRONTEND_DIR, BASE_DIR.parent / "frontend-dist", BASE_DIR / "frontend-dist"]:
    if _d is not None and _d.exists() and (_d / "index.html").exists():
        _FE_DIR = _d
        break

if _FE_DIR and (_FE_DIR / "index.html").exists():
    _assets_dir = _FE_DIR / "assets"
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    @app.get("/favicon.svg", include_in_schema=False)
    def serve_favicon():
        return FileResponse(str(_FE_DIR / "favicon.svg"))

    @app.get("/favicon.ico", include_in_schema=False)
    def serve_favicon_ico():
        return FileResponse(str(_FE_DIR / "favicon.ico"))

    @app.get("/app_icon.png", include_in_schema=False)
    def serve_app_icon_png():
        return FileResponse(str(_FE_DIR / "app_icon.png"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str = ""):
        """Serve SPA and set the local RPA token as an HttpOnly cookie."""
        if full_path.startswith("api/"):
            raise HTTPException(404)

        html_path = _FE_DIR / "index.html"
        html = html_path.read_text("utf-8")

        response = HTMLResponse(
            content=html,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
        response.set_cookie(
            "sy_local_token",
            LOCAL_TOKEN,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=60 * 60 * 24 * 365,
        )
        return response
else:
    @app.get("/{full_path:path}")
    async def serve_fallback(full_path: str = ""):
        if full_path.startswith("api/"):
            raise HTTPException(404)
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)


# ─── Main ──────────────────────────────────────────────────────────
def main():
    port = int(os.environ.get("PORT", "8766"))
    logger.info("Starting %s %s on port %s", APP_NAME, APP_VERSION, port)
    _record_event("startup", {"port": port, "arch": _windows_arch()})

    # PyInstaller --noconsole 模式下 stdout 可能为 None，需要禁用 uvicorn 彩色日志
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s %(message)s"},
            "access": {"format": "%(asctime)s %(levelname)s %(message)s"},
        },
        "handlers": {
            "default": {"formatter": "default", "class": "logging.StreamHandler", "stream": "ext://sys.stdout"},
            "access": {"formatter": "access", "class": "logging.StreamHandler", "stream": "ext://sys.stdout"},
            "file": {"formatter": "default", "class": "logging.FileHandler", "filename": str(LOG_PATH), "encoding": "utf-8"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default", "file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access", "file"], "level": "INFO", "propagate": False},
            "suixin.local": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }

    # Fix for PyInstaller: ensure stdout exists
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    import webbrowser

    def _open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://127.0.0.1:{port}")

    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", log_config=log_config)


if __name__ == "__main__":
    main()
