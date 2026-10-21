"""API FastAPI + serve o frontend HTML.  Correr:  uvicorn server:app --reload"""
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fin import ask, assets, categorize, db, ingest, kpis, llm, ocr

app = FastAPI(title="Finance Dashboard")
db.init()
WEB = Path(__file__).parent / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/meta")
def meta():
    with db.connect() as c:
        return {
            "accounts": [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY name")],
            "categories": [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY name")],
            "llm": llm.model(),
            "ocr_lang": ocr.ocr_lang(),
        }


@app.post("/api/import")
async def import_files(files: list[UploadFile] = File(...), account: str = Form("Conta principal"), account_type: str = Form("liquid")):
    results = []
    with tempfile.TemporaryDirectory() as tmp, db.connect() as c:
        for f in files:
            p = Path(tmp) / Path(f.filename).name
            with p.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            try:
                results.append(ingest.ingest_file(c, p, account.strip() or "Conta principal", account_type, display_name=f.filename))
            except Exception as e:  # erro por ficheiro, não aborta o lote
                results.append({"file": f.filename, "status": "error", "error": str(e)})
    return results


@app.get("/api/report")
def get_report(month: str | None = None, tax_pct: float = 0.25):
    with db.connect() as c:
        return kpis.report(db.load_transactions(c), db.load_table(c, "categories"), db.load_table(c, "accounts"), month, tax_pct)


@app.get("/api/transactions")
def transactions(month: str | None = None, category: str | None = None, account: str | None = None, q: str | None = None, limit: int = 500):
    sql, args = (
        "SELECT t.id, t.date, t.description, t.amount, t.category, t.cat_source, a.name AS account "
        "FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE 1=1",
        [],
    )
    if month:
        sql += " AND substr(t.date,1,7)=?"
        args.append(month)
    if category:
        sql += " AND t.category=?"
        args.append(category)
    if account:
        sql += " AND a.name=?"
        args.append(account)
    if q:
        sql += " AND t.description LIKE ?"
        args.append(f"%{q}%")
    sql += " ORDER BY t.date DESC, t.id DESC LIMIT ?"
    args.append(limit)
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, args)]


class CatUpdate(BaseModel):
    category: str
    create_rule: bool = False
    pattern: str | None = None


@app.patch("/api/transactions/{tid}")
def update_category(tid: int, body: CatUpdate):
    with db.connect() as c:
        row = c.execute("SELECT description FROM transactions WHERE id=?", (tid,)).fetchone()
        if not row:
            raise HTTPException(404)
        if body.create_rule:
            pat = categorize.norm(body.pattern or row["description"])
            c.execute("INSERT OR REPLACE INTO rules(pattern, category) VALUES (?,?)", (pat, body.category))
            c.execute("UPDATE transactions SET category=?, cat_source='manual' WHERE id=?", (body.category, tid))
            return {"rule": pat, **categorize.apply(c)}
        c.execute("UPDATE transactions SET category=?, cat_source='manual' WHERE id=?", (body.category, tid))
    return {"ok": True}


@app.post("/api/recategorize")
def recategorize():
    with db.connect() as c:
        return categorize.apply(c)


@app.get("/api/rules")
def rules():
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM rules ORDER BY category, pattern")]


class Rule(BaseModel):
    pattern: str
    category: str


@app.post("/api/rules")
def add_rule(r: Rule):
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO rules(pattern, category) VALUES (?,?)", (categorize.norm(r.pattern), r.category))
        return categorize.apply(c)


@app.delete("/api/rules/{rid}")
def delete_rule(rid: int):
    with db.connect() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rid,))
    return {"ok": True}


class AccountUpdate(BaseModel):
    opening_balance: float
    type: str


@app.put("/api/accounts/{aid}")
def update_account(aid: int, a: AccountUpdate):
    with db.connect() as c:
        c.execute("UPDATE accounts SET opening_balance=?, type=? WHERE id=?", (a.opening_balance, a.type, aid))
    return {"ok": True}


class AssetIn(BaseModel):
    name: str
    institution: str | None = None
    kind: str = "reserve"
    tanb: float | None = None
    note: str | None = None


class SnapshotIn(BaseModel):
    date: str
    value: float
    invested: float | None = None


def _check_kind(kind: str):
    if kind not in ("reserve", "investment"):
        raise HTTPException(422, "kind deve ser 'reserve' ou 'investment'")


@app.get("/api/assets")
def get_assets():
    with db.connect() as c:
        return assets.summary(c)


@app.post("/api/assets")
def add_asset(a: AssetIn):
    _check_kind(a.kind)
    with db.connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO assets(name, institution, kind, tanb, note) VALUES (?,?,?,?,?)",
                (a.name.strip(), a.institution, a.kind, a.tanb, a.note),
            )
        except Exception:
            raise HTTPException(409, "Já existe um ativo com esse nome")
        return {"id": cur.lastrowid}


@app.put("/api/assets/{aid}")
def update_asset(aid: int, a: AssetIn):
    _check_kind(a.kind)
    with db.connect() as c:
        c.execute(
            "UPDATE assets SET name=?, institution=?, kind=?, tanb=?, note=? WHERE id=?",
            (a.name.strip(), a.institution, a.kind, a.tanb, a.note, aid),
        )
    return {"ok": True}


@app.delete("/api/assets/{aid}")
def delete_asset(aid: int):
    with db.connect() as c:
        c.execute("DELETE FROM assets WHERE id=?", (aid,))
    return {"ok": True}


@app.post("/api/assets/{aid}/snapshots")
def add_snapshot(aid: int, s: SnapshotIn):
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM assets WHERE id=?", (aid,)).fetchone():
            raise HTTPException(404)
        c.execute(
            "INSERT INTO asset_snapshots(asset_id, date, value, invested) VALUES (?,?,?,?) "
            "ON CONFLICT(asset_id, date) DO UPDATE SET value=excluded.value, invested=excluded.invested",
            (aid, s.date, s.value, s.invested),
        )
    return {"ok": True}


@app.delete("/api/snapshots/{sid}")
def delete_snapshot(sid: int):
    with db.connect() as c:
        c.execute("DELETE FROM asset_snapshots WHERE id=?", (sid,))
    return {"ok": True}


class Question(BaseModel):
    question: str


@app.post("/api/ask")
def ask_data(q: Question):
    return ask.answer(q.question.strip())


@app.get("/api/imports")
def imports():
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM imports ORDER BY id DESC")]


class ImportRename(BaseModel):
    filename: str


@app.patch("/api/imports/{iid}")
def rename_import(iid: int, body: ImportRename):
    name = body.filename.strip()
    if not name:
        raise HTTPException(422, "O nome não pode ficar vazio")
    with db.connect() as c:
        c.execute("UPDATE imports SET filename=? WHERE id=?", (name, iid))
    return {"ok": True}


@app.delete("/api/imports/{iid}")
def delete_import(iid: int):
    with db.connect() as c:
        c.execute("DELETE FROM transactions WHERE import_id=?", (iid,))
        c.execute("DELETE FROM imports WHERE id=?", (iid,))
    return {"ok": True}
