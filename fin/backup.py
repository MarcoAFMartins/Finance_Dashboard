"""Cópias da base de dados: automáticas antes de operações que apagam ou mudam muito, e sob pedido.

Ficam em data/backups/ (ao lado da BD), com as KEEP mais recentes. Para repor: parar a app,
copiar o ficheiro escolhido por cima de data/finance.db e voltar a arrancar.
"""
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db

KEEP = int(os.environ.get("FIN_BACKUP_KEEP", "10"))


def backup_dir() -> Path:
    return db.db_path().parent / "backups"


def snapshot(reason: str = "manual") -> Path | None:
    """Copia a BD (API de backup do SQLite: consistente mesmo com a app a usá-la). None se ainda não há BD."""
    src = db.db_path()
    if not src.exists():
        return None
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^a-z0-9-]+", "-", reason.lower()).strip("-") or "manual"
    dest = d / f"finance-{datetime.now():%Y%m%d-%H%M%S-%f}-{tag}.db"
    a, b = sqlite3.connect(src), sqlite3.connect(dest)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    prune()
    return dest


def prune(keep: int = KEEP):
    files = list_backups()
    for f in files[keep:]:
        Path(f["path"]).unlink(missing_ok=True)


def list_backups() -> list[dict]:
    d = backup_dir()
    if not d.exists():
        return []
    files = sorted(d.glob("finance-*.db"), key=lambda p: p.name, reverse=True)
    return [
        {"name": p.name, "path": str(p), "size": p.stat().st_size,
         "created": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
        for p in files
    ]
