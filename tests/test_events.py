"""Eventos + simulação: migração, agregação por evento, projeção de saldo e API.  python tests/test_events.py"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp())
os.environ["FIN_DB"] = str(TMP / "test.db")
os.environ.pop("FIN_LLM", None)

from fin import db, events, ingest, kpis  # noqa: E402
from fin import simulate as sim  # noqa: E402

SAMPLES = ROOT / "data" / "samples"

OLD_SCHEMA = """
CREATE TABLE accounts(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, type TEXT NOT NULL DEFAULT 'liquid', opening_balance REAL NOT NULL DEFAULT 0);
CREATE TABLE categories(name TEXT PRIMARY KEY, kind TEXT NOT NULL, bucket TEXT NOT NULL DEFAULT 'other', fixed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE rules(id INTEGER PRIMARY KEY, pattern TEXT UNIQUE NOT NULL, category TEXT NOT NULL REFERENCES categories(name));
CREATE TABLE imports(id INTEGER PRIMARY KEY, filename TEXT, file_hash TEXT NOT NULL, method TEXT, account TEXT, rows_found INTEGER, rows_new INTEGER, imported_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(file_hash, account));
CREATE TABLE transactions(id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, date TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id), category TEXT NOT NULL DEFAULT 'Sem categoria', cat_source TEXT NOT NULL DEFAULT 'none',
    import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE);
CREATE VIEW v_tx AS SELECT id, date FROM transactions;
INSERT INTO accounts(name) VALUES ('Antiga');
INSERT INTO transactions(uid, date, description, amount, account_id) VALUES ('u1', '2026-01-05', 'ANTIGO', -10, 1);
"""


def test_migration():
    """Uma base criada antes dos eventos ganha event_id, mantém os dados e a vista nova."""
    old = TMP / "old.db"
    con = sqlite3.connect(old)
    con.executescript(OLD_SCHEMA)
    con.commit()
    con.close()
    prev = os.environ["FIN_DB"]
    os.environ["FIN_DB"] = str(old)
    try:
        db.init()
        db.init()  # idempotente
        with db.connect() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(transactions)")}
            assert "event_id" in cols
            row = c.execute("SELECT description, event_id, event FROM v_tx").fetchone()
            assert row["description"] == "ANTIGO" and row["event_id"] is None and row["event"] is None
    finally:
        os.environ["FIN_DB"] = prev
    print("migração OK")


def load(c):
    return db.load_transactions(c), db.load_table(c, "categories"), db.load_table(c, "accounts")


def test_recurring_on_its_day():
    """Renda fixa ao dia 1 sai inteira nesse dia; o resto continua espalhado; o total do mês não muda."""
    import pandas as pd

    rows = []
    for m in ("2026-01", "2026-02", "2026-03", "2026-04"):
        rows.append((f"{m}-01", "RENDA SENHORIO", -500.0, "Habitação"))
        rows.append((f"{m}-25", "SALARIO", 1500.0, "Rendimento"))
        for dd in range(2, 29, 3):
            rows.append((f"{m}-{dd:02d}", f"CONTINENTE {dd}{m}", -12.0 - dd, "Supermercado"))
    tx = pd.DataFrame(rows, columns=["date", "description", "amount", "category"])
    tx["date"] = pd.to_datetime(tx["date"])
    tx["id"], tx["cat_source"], tx["event_id"], tx["account"], tx["account_type"] = range(len(tx)), "rule", None, "C", "liquid"
    cats = pd.DataFrame(db.SEED_CATEGORIES, columns=["name", "kind", "bucket", "fixed"])
    acc = pd.DataFrame([{"name": "C", "type": "liquid", "opening_balance": 1000.0}])
    r = sim.run(tx, cats, acc, [], horizon_months=1, today=pd.Timestamp("2026-04-28"))
    b = r["baseline"]
    assert [(x["name"], x["day"], round(x["amount"])) for x in b["recurring"]] == [("RENDA SENHORIO", 1, 500)], b["recurring"]
    series = dict(zip(r["series"]["dates"], r["series"]["balance"]))  # mês a mês: fim de abril, fim de maio, fim de junho
    full = sim.run(tx, cats, acc, [], horizon_months=0, today=pd.Timestamp("2026-04-28"))
    daily = dict(zip(full["series"]["dates"], full["series"]["balance"]))
    assert abs(daily["2026-04-30"] - daily["2026-04-29"] + b["daily_outflow"]) < 0.01, "29→30 abril: só a parte variável"
    may = sim.run(tx, cats, acc, [], horizon_months=0, today=pd.Timestamp("2026-05-15"))
    d2 = dict(zip(may["series"]["dates"], may["series"]["balance"]))
    assert abs((d2["2026-05-01"] - d2["2026-04-30"]) - (-500 - b["daily_outflow"])) < 0.01, "a renda sai inteira ao dia 1"
    month_out = 500 + b["daily_outflow"] * 31
    assert abs(month_out - b["monthly_expenses"] * 31 / 30.4375) < 1.0 + 0.02 * month_out, (month_out, b["monthly_expenses"])
    assert series  # série mensal existe
    print("recorrentes no seu dia OK")


def test_backup():
    from fin import backup

    n_tx = None
    with db.connect() as c:
        n_tx = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    paths = [backup.snapshot("teste") for _ in range(backup.KEEP + 3)]
    lst = backup.list_backups()
    assert len(lst) == backup.KEEP, "só ficam as mais recentes"
    assert lst[0]["name"] == paths[-1].name and not paths[0].exists()
    with sqlite3.connect(paths[-1]) as b:
        assert b.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n_tx, "a cópia tem os mesmos dados"
    print("backups OK")


def run_sim(c, **kw):
    tx, cats, acc = load(c)
    return sim.run(tx, cats, acc, events.planned_for_sim(c), **kw)


def main():
    test_migration()
    db.init()
    with db.connect() as c:
        for f, acct in [("extrato_abr_ago.xlsx", "Conta"), ("cartao_junho.csv", "Cartão")]:
            assert ingest.ingest_file(c, SAMPLES / f, acct)["rows_new"] > 0

        # --- eventos: associar movimentos de categorias diferentes ---
        c.execute("INSERT INTO events(name, start_date, end_date, budget) VALUES ('Viagem', '2026-06-10', '2026-06-20', 600)")
        eid = c.execute("SELECT id FROM events WHERE name='Viagem'").fetchone()["id"]
        sug = events.suggestions(c, eid)
        assert sug and all("2026-06-10" <= s["date"] <= "2026-06-20" and s["amount"] < 0 for s in sug)
        cats_in_sug = {s["category"] for s in sug}
        assert len(cats_in_sug) >= 2, "o evento deve atravessar categorias"
        ids = [s["id"] for s in sug]
        assert events.assign(c, ids, eid) == len(ids)
        assert events.suggestions(c, eid) == [], "já associados não voltam a ser sugeridos"
        ev = events.list_events(c)["events"][0]
        assert ev["tx_count"] == len(ids)
        assert abs(ev["spent"] - (-sum(s["amount"] for s in sug))) < 0.01
        assert abs(sum(x["amount"] for x in ev["by_category"]) - ev["spent"]) < 0.01
        try:
            events.assign(c, ids, 9999)
            raise AssertionError("devia falhar com evento inexistente")
        except LookupError:
            pass

        # --- simulação sem itens: saldo inicial = liquid dos KPIs; base não conta movimentos de eventos ---
        tx, cats, acc = load(c)
        liquid = kpis.report(tx, cats, acc)["kpi"]["liquid"]
        r0 = run_sim(c, horizon_months=3)
        assert abs(r0["start_balance"] - liquid) < 0.01, (r0["start_balance"], liquid)
        base_with = r0["baseline"]["monthly_expenses"]
        events.assign(c, ids, None)
        base_without = run_sim(c, horizon_months=3)["baseline"]["monthly_expenses"]
        events.assign(c, ids, eid)
        assert base_without >= base_with, "movimentos de eventos saem da base"
        assert r0["planned_total"] == 0 and len(r0["series"]["dates"]) == len(r0["series"]["balance"]) == len(r0["series"]["has_item"])

        # --- horizonte: termina num fim de mês; 0 = este mês (diário), N = mês a mês ---
        import datetime as _dt
        last = _dt.date.fromisoformat(r0["start_date"])
        m0 = run_sim(c, horizon_months=0, today=last)
        nxt = (last.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        assert m0["step"] == "day" and m0["end_date"] == (nxt - _dt.timedelta(days=1)).isoformat(), m0["end_date"]
        assert len(m0["series"]["dates"]) == (nxt - last).days, "um ponto por dia até ao fim do mês"
        m3 = run_sim(c, horizon_months=3, today=last)
        assert m3["step"] == "month" and len(m3["series"]["dates"]) == 5, m3["series"]["dates"]  # início + 4 fins de mês
        assert all(_dt.date.fromisoformat(x).month != (_dt.date.fromisoformat(x) + _dt.timedelta(days=1)).month for x in m3["series"]["dates"][1:])
        assert m3["series"]["balance"][-1] == round(m3["final_balance"], 2)
        later = run_sim(c, horizon_months=0, today=last + _dt.timedelta(days=60))
        assert later["end_date"] > m0["end_date"], "'este mês' é o de hoje quando os dados estão atrasados"

        # --- itens planeados: ponderação por certeza ---
        start = r0["start_date"]
        d = lambda n: (__import__("datetime").date.fromisoformat(start) + __import__("datetime").timedelta(days=n)).isoformat()  # noqa: E731
        c.execute("INSERT INTO events(name, start_date, end_date) VALUES ('Algarve', ?, ?)", (d(20), d(25)))
        aid = c.execute("SELECT id FROM events WHERE name='Algarve'").fetchone()["id"]
        for desc, amt, day, cert, cat in [
            ("Alojamento", -300, 21, "certain", "Viagens"),
            ("Jantares", -200, 22, "likely", "Restauração"),
            ("Extras", -100, 23, "maybe", None),
            ("Reembolso", 50, 30, "likely", None),
            ("Já passou", -999, -3, "certain", None),
        ]:
            c.execute("INSERT INTO planned_items(event_id, description, amount, date, category, certainty) VALUES (?,?,?,?,?,?)", (aid, desc, amt, d(day), cat, cert))
        exp = run_sim(c, horizon_months=3)
        assert abs(exp["planned_total"] - (-300 - 140 - 30 + 35)) < 0.01, exp["planned_total"]
        assert abs((exp["final_balance"] - r0["final_balance"]) - exp["planned_total"]) < 0.02
        assert exp["ignored"] == 1, "itens com data passada ficam de fora"
        pes = run_sim(c, horizon_months=3, mode="pessimistic")
        assert abs(pes["planned_total"] - (-600)) < 0.01, "pessimista: despesas inteiras, entrada incerta a 0"
        assert pes["final_balance"] < exp["final_balance"]
        assert exp["impact"][0]["event"] == "Algarve"

        # --- cenário: excluir o evento ---
        c.execute("UPDATE events SET include_in_sim=0 WHERE id=?", (aid,))
        off = run_sim(c, horizon_months=3)
        assert off["planned_total"] == 0 and abs(off["final_balance"] - r0["final_balance"]) < 0.01
        c.execute("UPDATE events SET include_in_sim=1 WHERE id=?", (aid,))

        # --- limite ---
        high = run_sim(c, horizon_months=3, floor=exp["min_balance"] + 1000)
        assert high["below_floor_date"] is not None
        low = run_sim(c, horizon_months=3, floor=exp["min_balance"] - 1000)
        assert low["below_floor_date"] is None

        # --- objetivos: poupado = crescimento do saldo (+ investido); data além do horizonte também conta ---
        far = _dt.date.fromisoformat(start) + _dt.timedelta(days=200)
        m0 = run_sim(c, horizon_months=0, today=last)  # de novo: agora já há itens planeados
        gl = run_sim(c, horizon_months=0, today=last, goals=[
            {"id": 1, "name": "Fim do mês", "target": 0.01, "date": m0["end_date"]},
            {"id": 2, "name": "Impossível", "target": 10_000_000, "date": far.isoformat()},
            {"id": 3, "name": "Passado", "target": 100, "date": d(-5)},
            {"id": 4, "name": "Longe", "target": 100, "date": "2099-01-01"},
        ])
        st = {g["name"]: g for g in gl["goals"]}
        assert gl["end_date"] == m0["end_date"], "objetivos não mudam o horizonte mostrado"
        assert len(gl["series"]["dates"]) == len(m0["series"]["dates"])
        g1, days = st["Fim do mês"], (_dt.date.fromisoformat(m0["end_date"]) - last).days
        invested = gl["baseline"]["monthly_invested"] / 30.4375 * days
        assert abs(g1["projected"] - (m0["final_balance"] - m0["start_balance"] + invested)) < 0.01, "poupado = crescimento do saldo + investido"
        assert (g1["status"] == "ok") == (g1["projected"] >= 0.01)
        nosave = run_sim(c, horizon_months=0, today=last, include_saving=False, goals=[{"name": "x", "target": 1, "date": m0["end_date"]}])
        assert abs(nosave["goals"][0]["projected"] - (nosave["final_balance"] - nosave["start_balance"])) < 0.01
        assert st["Impossível"]["status"] == "short" and st["Impossível"]["gap"] > 0
        assert st["Passado"]["status"] == "past" and st["Longe"]["status"] == "too_far"
        assert abs(st["Impossível"]["needed_per_month"] * st["Impossível"]["months"] - 10_000_000) < 10_000  # valores saem arredondados

        # --- eventos: gasto do mês ---
        month = c.execute("SELECT substr(date,1,7) m FROM transactions WHERE event_id=? LIMIT 1", (eid,)).fetchone()["m"]
        em = next(e for e in events.list_events(c, month)["events"] if e["id"] == eid)
        assert 0 < em["spent_month"] <= em["spent"] + 0.01
        assert "spent_month" not in events.list_events(c)["events"][0]

        # --- categorias próprias ---
        from fin import categories
        categories.create(c, "Animais", "expense", "wants", False)
        for bad in [("animais", "expense", "wants"), ("", "expense", "wants"), ("X", "xpto", "wants"), ("Y", "expense", "xpto")]:
            try:
                categories.create(c, *bad)
                raise AssertionError(f"devia falhar: {bad}")
            except (ValueError, FileExistsError):
                pass
        tid = c.execute("SELECT id FROM transactions LIMIT 1").fetchone()["id"]
        c.execute("UPDATE transactions SET category='Animais', cat_source='manual' WHERE id=?", (tid,))
        c.execute("INSERT INTO rules(pattern, category) VALUES ('petshop', 'Animais')")
        categories.update(c, "Animais", "expense", "needs", True)
        assert dict(c.execute("SELECT bucket, fixed, custom FROM categories WHERE name='Animais'").fetchone()) == {"bucket": "needs", "fixed": 1, "custom": 1}
        try:
            categories.delete(c, "Supermercado")
            raise AssertionError("categorias de origem não se apagam")
        except PermissionError:
            pass
        assert categories.delete(c, "Animais") == 1
        assert c.execute("SELECT category, cat_source FROM transactions WHERE id=?", (tid,)).fetchone()["category"] == "Sem categoria"
        assert not c.execute("SELECT 1 FROM rules WHERE category='Animais'").fetchone()
        assert not c.execute("SELECT 1 FROM categories WHERE name='Animais'").fetchone()

        # --- apagar evento: movimentos ficam, itens planeados vão com ele ---
        n_tx = c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
        c.execute("DELETE FROM events WHERE id=?", (aid,))
        assert c.execute("SELECT COUNT(*) n FROM planned_items").fetchone()["n"] == 0
        c.execute("DELETE FROM events WHERE id=?", (eid,))
        assert c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == n_tx
        assert c.execute("SELECT COUNT(*) n FROM transactions WHERE event_id IS NOT NULL").fetchone()["n"] == 0
    print("eventos + simulação OK")
    test_recurring_on_its_day()
    test_backup()
    test_api()
    print("TUDO OK")


def test_api():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("API: saltado (falta httpx)")
        return
    import server

    cl = TestClient(server.app)
    with db.connect() as c:
        ingest.ingest_file(c, SAMPLES / "extrato_abr_ago.xlsx", "Conta")
    start = cl.get("/api/simulate?horizon_months=2").json()["start_date"]
    from datetime import date, timedelta

    when = (date.fromisoformat(start) + timedelta(days=10)).isoformat()

    r = cl.post("/api/events", json={"name": "Concerto", "start_date": when, "end_date": when, "budget": 120})
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    assert cl.post("/api/events", json={"name": "Concerto"}).status_code == 409
    assert cl.post("/api/events", json={"name": "X", "start_date": "2026-02-30"}).status_code == 422
    assert cl.post("/api/events", json={"name": "X", "start_date": "2026-05-02", "end_date": "2026-05-01"}).status_code == 422

    p = {"event_id": eid, "description": "Bilhetes", "amount": 80, "date": when, "category": "Lazer", "certainty": "likely"}
    assert cl.post("/api/planned", json=p).status_code == 200
    assert cl.post("/api/planned", json={**p, "amount": -5}).status_code == 422
    assert cl.post("/api/planned", json={**p, "category": "Inventada"}).status_code == 422
    assert cl.post("/api/planned", json={**p, "event_id": 9999}).status_code == 422
    assert cl.get("/api/simulate?mode=xpto").status_code == 422

    s = cl.get("/api/simulate?horizon_months=2").json()
    assert abs(s["planned_total"] - (-80 * 0.7)) < 0.01 and s["items"][0]["amount"] == -80
    lst = cl.get("/api/events").json()
    assert lst["events"][0]["planned_total"] == 80 and lst["loose"] == []

    # atribuição em lote + filtro por evento + vista para o Perguntar
    tx = cl.get("/api/transactions?limit=3").json()
    ids = [t["id"] for t in tx]
    assert cl.post("/api/transactions/event", json={"tx_ids": ids, "event_id": eid}).json()["updated"] == 3
    assert len(cl.get("/api/transactions", params={"event_id": eid}).json()) == 3
    assert cl.post("/api/transactions/event", json={"tx_ids": ids, "event_id": 9999}).status_code == 404
    from fin import ask
    cols, rows = ask.run_readonly("SELECT event, COUNT(*) FROM v_tx WHERE event IS NOT NULL GROUP BY event")
    assert rows == [["Concerto", 3]], rows

    # categorias e objetivos pela API
    assert cl.post("/api/categories", json={"name": "Animais", "kind": "expense", "bucket": "wants"}).status_code == 200
    assert cl.post("/api/categories", json={"name": "Animais"}).status_code == 409
    assert cl.post("/api/categories", json={"name": "Z", "kind": "xpto"}).status_code == 422
    assert cl.put("/api/categories/Animais", json={"name": "Animais", "kind": "expense", "bucket": "needs", "fixed": True}).status_code == 200
    assert cl.delete("/api/categories/Supermercado").status_code == 403
    assert cl.delete("/api/categories/Animais").json() == {"moved": 0}
    assert cl.post("/api/goals", json={"name": "Carro", "target": -1, "date": "2027-01-01"}).status_code == 422
    gid = cl.post("/api/goals", json={"name": "Carro", "target": 500, "date": "2099-01-01"}).json()["id"]
    assert cl.get("/api/simulate").json()["goals"][0]["status"] == "too_far"
    assert cl.delete(f"/api/goals/{gid}").status_code == 200 and cl.get("/api/goals").json() == []

    assert cl.delete(f"/api/events/{eid}").status_code == 200
    assert cl.get("/api/events").json() == {"events": [], "loose": []}
    print("API OK")


if __name__ == "__main__":
    main()
