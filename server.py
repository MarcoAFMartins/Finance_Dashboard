"""API FastAPI + serve o frontend HTML.  Correr:  uvicorn server:app --reload"""
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fin import ask, assets, categorize, db, events, ingest, kpis, llm, ocr
from fin import simulate as sim

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
def transactions(month: str | None = None, category: str | None = None, account: str | None = None, q: str | None = None, event_id: int | None = None, limit: int = 500):
    sql, args = (
        "SELECT t.id, t.date, t.description, t.amount, t.category, t.cat_source, a.name AS account, t.event_id, e.name AS event "
        "FROM transactions t JOIN accounts a ON a.id=t.account_id LEFT JOIN events e ON e.id=t.event_id WHERE 1=1",
        [],
    )
    if event_id:
        sql += " AND t.event_id=?"
        args.append(event_id)
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


class EventIn(BaseModel):
    name: str
    start_date: str | None = None
    end_date: str | None = None
    budget: float | None = None
    status: str = "planned"
    include_in_sim: bool = True


def _event_args(e: EventIn):
    name = e.name.strip()
    if not name:
        raise HTTPException(422, "O evento precisa de nome")
    if e.status not in events.STATUSES:
        raise HTTPException(422, "status deve ser planned, active ou done")
    try:
        start, end = events.check_iso(e.start_date), events.check_iso(e.end_date)
    except ValueError:
        raise HTTPException(422, "Datas devem ser yyyy-mm-dd")
    if start and end and end < start:
        raise HTTPException(422, "A data de fim é anterior à de início")
    return name, start, end, e.budget, e.status, int(e.include_in_sim)


@app.get("/api/events")
def get_events():
    with db.connect() as c:
        return events.list_events(c)


@app.post("/api/events")
def add_event(e: EventIn):
    args = _event_args(e)
    with db.connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO events(name, start_date, end_date, budget, status, include_in_sim) VALUES (?,?,?,?,?,?)", args
            )
        except Exception:
            raise HTTPException(409, "Já existe um evento com esse nome")
        return {"id": cur.lastrowid}


@app.put("/api/events/{eid}")
def update_event(eid: int, e: EventIn):
    args = _event_args(e)
    with db.connect() as c:
        try:
            cur = c.execute(
                "UPDATE events SET name=?, start_date=?, end_date=?, budget=?, status=?, include_in_sim=? WHERE id=?", (*args, eid)
            )
        except Exception:
            raise HTTPException(409, "Já existe um evento com esse nome")
        if not cur.rowcount:
            raise HTTPException(404)
    return {"ok": True}


@app.delete("/api/events/{eid}")
def delete_event(eid: int):
    # os movimentos ficam (event_id passa a NULL); os itens planeados do evento apagam-se em cascata
    with db.connect() as c:
        c.execute("DELETE FROM events WHERE id=?", (eid,))
    return {"ok": True}


@app.get("/api/events/{eid}/suggestions")
def event_suggestions(eid: int):
    with db.connect() as c:
        return events.suggestions(c, eid)


class AssignIn(BaseModel):
    tx_ids: list[int]
    event_id: int | None = None  # None = tirar do evento


@app.post("/api/transactions/event")
def assign_event(a: AssignIn):
    with db.connect() as c:
        try:
            return {"updated": events.assign(c, a.tx_ids, a.event_id)}
        except LookupError as e:
            raise HTTPException(404, str(e))


class PlannedIn(BaseModel):
    event_id: int | None = None
    description: str
    amount: float          # sempre positivo; o sentido vem de kind
    kind: str = "expense"  # expense | income
    date: str
    category: str | None = None
    certainty: str = "certain"


def _planned_args(p: PlannedIn):
    if not p.description.strip():
        raise HTTPException(422, "Descrição vazia")
    if p.amount <= 0:
        raise HTTPException(422, "O valor deve ser positivo (usa 'kind' para entrada/despesa)")
    if p.kind not in ("expense", "income"):
        raise HTTPException(422, "kind deve ser expense ou income")
    if p.certainty not in events.CERTAINTIES:
        raise HTTPException(422, "certainty deve ser certain, likely ou maybe")
    try:
        d = events.check_iso(p.date)
    except ValueError:
        d = None
    if not d:
        raise HTTPException(422, "Data deve ser yyyy-mm-dd")
    signed = -p.amount if p.kind == "expense" else p.amount
    return (p.event_id, p.description.strip(), signed, d, p.category or None, p.certainty)


@app.post("/api/planned")
def add_planned(p: PlannedIn):
    args = _planned_args(p)
    with db.connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO planned_items(event_id, description, amount, date, category, certainty) VALUES (?,?,?,?,?,?)", args
            )
        except Exception:
            raise HTTPException(422, "Evento ou categoria inexistente")
        return {"id": cur.lastrowid}


@app.put("/api/planned/{pid}")
def update_planned(pid: int, p: PlannedIn):
    args = _planned_args(p)
    with db.connect() as c:
        try:
            cur = c.execute(
                "UPDATE planned_items SET event_id=?, description=?, amount=?, date=?, category=?, certainty=? WHERE id=?", (*args, pid)
            )
        except Exception:
            raise HTTPException(422, "Evento ou categoria inexistente")
        if not cur.rowcount:
            raise HTTPException(404)
    return {"ok": True}


@app.delete("/api/planned/{pid}")
def delete_planned(pid: int):
    with db.connect() as c:
        c.execute("DELETE FROM planned_items WHERE id=?", (pid,))
    return {"ok": True}


@app.get("/api/simulate")
def simulate(horizon_months: int = 0, floor: float = 0.0, mode: str = "expected", include_saving: bool = True):
    if mode not in sim.MODES:
        raise HTTPException(422, "mode deve ser expected ou pessimistic")
    with db.connect() as c:
        return sim.run(
            db.load_transactions(c), db.load_table(c, "categories"), db.load_table(c, "accounts"),
            events.planned_for_sim(c), max(0, min(horizon_months, 12)), floor, mode, include_saving,
        )


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
