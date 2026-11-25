"""개발용 로컬 PostgreSQL 16 관리 스크립트 (Docker 미설치 Windows/macOS/Linux).

pip 패키지 `pgserver` 에 포함된 PostgreSQL 바이너리를 사용한다.
    python infrastructure/scripts/dev-db.py start|stop|status|reset

운영/스테이징은 docker-compose 의 postgres 서비스를 사용한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / ".devdata" / "pg"
LOG = ROOT / ".devdata" / "pg.log"
PORT = int(os.environ.get("DEV_DB_PORT", "55432"))
USER = "foodmes"
PASSWORD = os.environ.get("DEV_DB_PASSWORD", "foodmes")
DATABASES = ["foodmes", "foodmes_test"]


def bin_dir() -> Path:
    import pgserver  # type: ignore

    return Path(pgserver.__file__).parent / "pginstall" / "bin"


def run(exe: str, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    path = bin_dir() / (exe + (".exe" if os.name == "nt" else ""))
    return subprocess.run([str(path), *args], check=check, capture_output=capture, text=True)


def psql(sql: str, db: str = "postgres") -> str:
    r = run("psql", "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", "-d", db, "-tAc", sql, capture=True)
    return r.stdout.strip()


def status() -> bool:
    if not DATA.exists():
        return False
    r = run("pg_ctl", "-D", str(DATA), "status", check=False, capture=True)
    return r.returncode == 0


def start() -> None:
    if not (DATA / "PG_VERSION").exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        run("initdb", "-D", str(DATA), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--locale=C")
    if not status():
        run("pg_ctl", "-D", str(DATA), "-l", str(LOG), "-o", f"-p {PORT} -h 127.0.0.1", "-w", "start")
    if psql(f"SELECT 1 FROM pg_roles WHERE rolname='{USER}'") != "1":
        psql(f"CREATE ROLE {USER} LOGIN PASSWORD '{PASSWORD}' CREATEDB")
    for db in DATABASES:
        if psql(f"SELECT 1 FROM pg_database WHERE datname='{db}'") != "1":
            psql(f"CREATE DATABASE {db} OWNER {USER} ENCODING 'UTF8' TEMPLATE template0")
    print(f"PostgreSQL running: postgresql://{USER}:***@127.0.0.1:{PORT}/foodmes")


def stop() -> None:
    if status():
        run("pg_ctl", "-D", str(DATA), "stop", "-m", "fast")
    print("stopped")


def reset() -> None:
    stop()
    shutil.rmtree(DATA, ignore_errors=True)
    start()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    {"start": start, "stop": stop, "reset": reset, "status": lambda: print("running" if status() else "stopped")}[cmd]()
