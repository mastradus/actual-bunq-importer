#!/usr/bin/env python3
"""
app.py — FastAPI Web UI for bunq → Actual Budget Sync
"""

import json
import logging
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup
from pydantic import BaseModel
from starlette.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config" / "config.json"
LOG_FILE = BASE_DIR / "logs" / "sync.log"
JOBS_DB = BASE_DIR / "data" / "jobs.db"
CRONTAB_FILE = BASE_DIR / "crontab"
SYNC_SCRIPT = BASE_DIR / "sync.py"

# Make sure project modules (bunq_client, actual_client, …) are importable
sys.path.insert(0, str(BASE_DIR))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App & templates
# ---------------------------------------------------------------------------

app = FastAPI(title="bunq → Actual Sync")

# Build a custom Jinja2 Environment with cache disabled.
# Jinja2 3.1.5+ computes the cache key via frozenset(globals.items()), which
# fails with "unhashable type: dict" when template context contains dicts.
# cache_size=0 avoids that entirely — acceptable for a low-traffic admin UI
# and required anyway since templates/ is volume-mounted for live editing.
_jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    cache_size=0,
    auto_reload=True,
)
_jinja_env.filters["tojson"] = lambda v: Markup(
    json.dumps(v, ensure_ascii=False)
    .replace("<", "\\u003c")
    .replace(">", "\\u003e")
    .replace("&", "\\u0026")
)
templates = Jinja2Templates(env=_jinja_env)

# ---------------------------------------------------------------------------
# In-memory process state (single-process, not survived across restarts)
# ---------------------------------------------------------------------------

_current_process: Optional[subprocess.Popen] = None
_current_job_id: Optional[int] = None
_process_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db() -> None:
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(JOBS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT    NOT NULL,
            finished_at TEXT,
            job_type    TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'running',
            imported    INTEGER DEFAULT 0,
            skipped     INTEGER DEFAULT 0,
            errors      INTEGER DEFAULT 0,
            output      TEXT    DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


def db_create_job(job_type: str) -> int:
    conn = sqlite3.connect(JOBS_DB)
    cursor = conn.execute(
        "INSERT INTO jobs (started_at, job_type, status) VALUES (?, ?, 'running')",
        (_now().isoformat(),  job_type),
    )
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return job_id


def db_finish_job(
    job_id: int,
    status: str,
    output: str,
    imported: int = 0,
    skipped: int = 0,
    errors: int = 0,
) -> None:
    conn = sqlite3.connect(JOBS_DB)
    conn.execute(
        """UPDATE jobs
           SET finished_at=?, status=?, output=?, imported=?, skipped=?, errors=?
           WHERE id=?""",
        (_now().isoformat(), status, output, imported, skipped, errors, job_id),
    )
    conn.commit()
    conn.close()


def db_get_recent_jobs(limit: int = 10) -> list[dict]:
    conn = sqlite3.connect(JOBS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_orphan_cleanup() -> None:
    """Mark jobs that were 'running' when the server last stopped as errors."""
    conn = sqlite3.connect(JOBS_DB)
    conn.execute(
        "UPDATE jobs SET status='error', output='Server restarted while job was running' "
        "WHERE status='running'"
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _now() -> datetime:
    """Return current datetime in the timezone from config (default: Europe/Berlin)."""
    tz_name = load_config().get("sync", {}).get("timezone", "Europe/Berlin")
    try:
        return datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        return datetime.now(ZoneInfo("Europe/Berlin"))


def config_safe_for_ui(config: dict) -> dict:
    """Return a deep copy of config with secret fields masked."""
    import copy

    c = copy.deepcopy(config)
    bunq_secrets = ("api_key", "installation_token", "device_token", "private_key", "server_public_key")
    actual_secrets = ("password", "encryption_password")
    for field in bunq_secrets:
        if c.get("bunq", {}).get(field):
            c["bunq"][field] = "••••••••"
    for field in actual_secrets:
        if c.get("actual", {}).get(field):
            c["actual"][field] = "••••••••"
    return c


# ---------------------------------------------------------------------------
# Cron helpers
# ---------------------------------------------------------------------------

CRON_PRESETS: dict[str, str] = {
    "*/15 * * * *": "Every 15 minutes",
    "*/30 * * * *": "Every 30 minutes",
    "0 * * * *":    "Every hour",
    "0 */2 * * *":  "Every 2 hours",
    "0 */4 * * *":  "Every 4 hours",
    "0 */6 * * *":  "Every 6 hours",
    "0 */12 * * *": "Twice a day",
    "0 8 * * *":    "Daily at 08:00",
    "0 20 * * *":   "Daily at 20:00",
}


def describe_cron(expr: str) -> str:
    return CRON_PRESETS.get(expr, expr)


def write_crontab(schedule: str, enabled: bool = True) -> None:
    """Rewrite the crontab file and signal supercronic to reload.

    When enabled=False the file is left empty so supercronic idles;
    the schedule string is preserved in config.json for re-enabling later.
    """
    if enabled:
        CRONTAB_FILE.write_text(
            f"{schedule} cd {BASE_DIR} && python3 {SYNC_SCRIPT}\n"
        )
        logger.info(f"Crontab enabled: {schedule}")
    else:
        CRONTAB_FILE.write_text("# sync disabled\n")
        logger.info("Crontab disabled — supercronic will idle")
    try:
        subprocess.run(["pkill", "-HUP", "supercronic"], capture_output=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def _parse_sync_output(output: str) -> dict[str, int]:
    """Extract imported/skipped/errors counts from sync.py log output."""
    imported = skipped = errors = 0
    m = re.search(r"(\d+) imported.*?(\d+) skipped.*?(\d+) errors", output)
    if m:
        imported, skipped, errors = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return {"imported": imported, "skipped": skipped, "errors": errors}


def _run_sync_in_thread(job_id: int, extra_args: list[str]) -> None:
    global _current_process, _current_job_id

    cmd = ["python3", str(SYNC_SCRIPT)] + extra_args
    logger.info(f"Job {job_id} starting: {' '.join(cmd)}")

    try:
        with _process_lock:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(BASE_DIR),
            )
            _current_process = proc
            _current_job_id = job_id

        lines: list[str] = []
        for line in proc.stdout:
            lines.append(line.rstrip())

        proc.wait()
        output = "\n".join(lines)
        status = "success" if proc.returncode == 0 else "error"
        counts = _parse_sync_output(output)
        db_finish_job(job_id, status, output, **counts)
        logger.info(f"Job {job_id} finished: {status}")

    except Exception as exc:
        logger.error(f"Job {job_id} failed: {exc}")
        db_finish_job(job_id, "error", str(exc))

    finally:
        with _process_lock:
            _current_process = None
            _current_job_id = None


def start_job(job_type: str, args: list[str]) -> int:
    global _current_process

    with _process_lock:
        if _current_process is not None:
            raise RuntimeError("A job is already running")

    job_id = db_create_job(job_type)
    t = threading.Thread(target=_run_sync_in_thread, args=(job_id, args), daemon=True)
    t.start()
    return job_id


def current_status() -> dict:
    with _process_lock:
        running = _current_process is not None
        current_id = _current_job_id

    jobs = db_get_recent_jobs(1)
    last_job = jobs[0] if jobs else None

    if running:
        status = "running"
    elif last_job and last_job["status"] == "error":
        status = "error"
    else:
        status = "idle"

    cfg = load_config()
    sync_enabled = cfg.get("sync", {}).get("enabled", True)

    return {
        "status": status,
        "current_job_id": current_id,
        "last_job": last_job,
        "sync_enabled": sync_enabled,
    }


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    init_db()
    db_orphan_cleanup()
    # Sync crontab with config on startup
    config = load_config()
    schedule = config.get("sync", {}).get("cron_schedule", "*/30 * * * *")
    enabled = config.get("sync", {}).get("enabled", True)
    write_crontab(schedule, enabled)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    config = load_config()
    return templates.TemplateResponse(request, "index.html", {
        "page":   "dashboard",
        "status": current_status(),
        "jobs":   db_get_recent_jobs(10),
        "config": config,
    })


@app.get("/provider", response_class=HTMLResponse)
async def page_provider(request: Request):
    config = load_config()
    return templates.TemplateResponse(request, "index.html", {
        "page":   "provider",
        "config": config,
    })


@app.get("/accounts", response_class=HTMLResponse)
async def page_accounts(request: Request):
    config = load_config()
    return templates.TemplateResponse(request, "index.html", {
        "page":        "accounts",
        "config":      config,
        "account_map": config.get("sync", {}).get("account_map", {}),
    })


@app.get("/config", response_class=HTMLResponse)
async def page_config(request: Request):
    config = load_config()
    cron = config.get("sync", {}).get("cron_schedule", "*/30 * * * *")
    return templates.TemplateResponse(request, "index.html", {
        "page":             "config",
        "config":           config,
        "cron_description": describe_cron(cron),
        "cron_presets":     CRON_PRESETS,
    })


@app.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "page":   "logs",
        "config": load_config(),
    })


# ---------------------------------------------------------------------------
# API — status & logs
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def api_status():
    return current_status()


@app.get("/api/logs/stream")
async def api_logs_stream(lines: int = 100):
    if not LOG_FILE.exists():
        return {"lines": []}
    all_lines = LOG_FILE.read_text(errors="replace").splitlines()
    return {"lines": all_lines[-lines:]}


# ---------------------------------------------------------------------------
# API — sync operations
# ---------------------------------------------------------------------------


@app.post("/api/sync/run")
async def api_sync_run(full: bool = False, since: str | None = None):
    args: list[str] = []
    if full:
        args.append("--full")
    if since:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", since):
            raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD")
        args += ["--since", since]

    job_type = "full_sync" if full else "sync"
    try:
        job_id = start_job(job_type, args)
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/sync/enabled")
async def api_sync_set_enabled(enabled: bool):
    config = load_config()
    if "sync" not in config:
        config["sync"] = {}
    config["sync"]["enabled"] = enabled
    save_config(config)
    schedule = config["sync"].get("cron_schedule", "*/30 * * * *")
    write_crontab(schedule, enabled)
    return {"sync_enabled": enabled}


@app.post("/api/setup/run")
async def api_setup_run():
    try:
        job_id = start_job("setup", ["--setup"])
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/accounts/init")
async def api_accounts_init(off_budget: bool = False):
    args = ["--init-accounts"] + (["--off-budget"] if off_budget else [])
    try:
        job_id = start_job("init_accounts", args)
        return {"job_id": job_id, "status": "started"}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---------------------------------------------------------------------------
# API — config
# ---------------------------------------------------------------------------


class ProviderSelectBody(BaseModel):
    provider: str


@app.post("/api/provider/select")
async def api_provider_select(body: ProviderSelectBody):
    config = load_config()
    config["provider"] = body.provider
    save_config(config)
    return {"status": "ok"}


class BunqConfigBody(BaseModel):
    api_key: str | None = None
    device_description: str | None = None


@app.post("/api/provider/config")
async def api_provider_config(body: BunqConfigBody):
    config = load_config()
    if "bunq" not in config:
        config["bunq"] = {}
    if body.api_key and "•" not in body.api_key:
        config["bunq"]["api_key"] = body.api_key
    if body.device_description:
        config["bunq"]["device_description"] = body.device_description
    save_config(config)
    return {"status": "ok"}


class ActualConfigBody(BaseModel):
    url: str | None = None
    password: str | None = None
    budget_name: str | None = None
    encryption_password: str | None = None
    since_date: str | None = None
    cron_schedule: str | None = None
    timezone: str | None = None


@app.post("/api/config/save")
async def api_config_save(body: ActualConfigBody):
    config = load_config()
    if "actual" not in config:
        config["actual"] = {}
    if "sync" not in config:
        config["sync"] = {}

    if body.url:
        config["actual"]["url"] = body.url
    if body.password and "•" not in body.password:
        config["actual"]["password"] = body.password
    if body.budget_name:
        config["actual"]["budget_name"] = body.budget_name
    if body.encryption_password and "•" not in body.encryption_password:
        config["actual"]["encryption_password"] = body.encryption_password
    # data_dir is fixed to the data volume — not user-configurable
    config["actual"]["data_dir"] = "/app/data/actual-cache"
    if body.since_date:
        config["sync"]["since_date"] = body.since_date
    if body.cron_schedule:
        config["sync"]["cron_schedule"] = body.cron_schedule
        write_crontab(body.cron_schedule)
    if body.timezone:
        try:
            ZoneInfo(body.timezone)          # validate before saving
            config["sync"]["timezone"] = body.timezone
        except ZoneInfoNotFoundError:
            raise HTTPException(status_code=400, detail=f"Unknown timezone: {body.timezone}")

    save_config(config)
    return {"status": "ok"}


class AccountMapBody(BaseModel):
    account_map: dict


@app.post("/api/accounts/map")
async def api_accounts_map(body: AccountMapBody):
    config = load_config()
    if "sync" not in config:
        config["sync"] = {}
    config["sync"]["account_map"] = body.account_map
    config["sync"]["_resolved_account_map"] = {}   # force re-resolution on next sync
    save_config(config)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API — live account fetching
# ---------------------------------------------------------------------------


@app.get("/api/accounts/bunq")
async def api_accounts_bunq():
    try:
        config = load_config()
        if not config.get("bunq", {}).get("installation_token"):
            return {"error": "bunq not set up yet — run Setup first", "accounts": []}

        import bunq_client

        session_token, user_id = bunq_client.create_session(config)
        accounts = bunq_client.get_monetary_accounts(session_token, user_id)
        return {"accounts": accounts}
    except Exception as exc:
        logger.exception("api_accounts_bunq failed")
        return {"error": str(exc), "accounts": []}


@app.get("/api/accounts/actual")
async def api_accounts_actual():
    try:
        config = load_config()
        cfg = config.get("actual", {})
        if not cfg.get("password"):
            return {"error": "Actual Budget not configured", "accounts": []}

        from actual_client import ActualClient

        client = ActualClient(
            base_url=cfg["url"],
            password=cfg["password"],
            budget_name=cfg["budget_name"],
            cert=cfg.get("cert", False),
            encryption_password=cfg.get("encryption_password"),
            data_dir=cfg.get("data_dir"),
        )
        return {"accounts": client.list_accounts()}
    except Exception as exc:
        logger.exception("api_accounts_actual failed")
        return {"error": str(exc), "accounts": []}


@app.post("/api/provider/test")
async def api_provider_test():
    try:
        config = load_config()
        if not config.get("bunq", {}).get("installation_token"):
            return {"success": False, "message": "bunq not set up — run Setup first"}

        import bunq_client

        session_token, user_id = bunq_client.create_session(config)
        accounts = bunq_client.get_monetary_accounts(session_token, user_id)
        return {
            "success": True,
            "message": f"Connected! Found {len(accounts)} active account(s).",
        }
    except Exception as exc:
        logger.exception("api_provider_test failed")
        return {"success": False, "message": str(exc)}


@app.get("/api/cron/describe")
async def api_cron_describe(expr: str):
    return {"description": describe_cron(expr)}
