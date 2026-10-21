"""Reserva e investimentos: totais, TANB, histórico, isolamento dos KPIs.  python tests/test_assets.py"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["FIN_DB"] = str(Path(tempfile.mkdtemp()) / "assets.db")

from fin import assets, db, ingest, kpis  # noqa: E402


def main():
    db.init()
    with db.connect() as c:
        c.execute("INSERT INTO assets(name, institution, kind, tanb) VALUES ('Conta Reserva','Banco X','reserve',2.5)")
        c.execute("INSERT INTO assets(name, institution, kind) VALUES ('ETF Mundo','Corretora Y','investment')")
        for aid, d, v, inv in [(1, "2026-08-01", 4000, None), (1, "2026-10-01", 5000, None), (2, "2026-09-01", 1000, 900), (2, "2026-10-01", 1200, 1000)]:
            c.execute("INSERT INTO asset_snapshots(asset_id, date, value, invested) VALUES (?,?,?,?)", (aid, d, v, inv))
        s = assets.summary(c)
        t = s["totals"]
        assert t["reserve"] == 5000 and t["investment"] == 1200 and t["total"] == 6200, t
        assert abs(t["est_interest_year"] - 125.0) < 1e-9 and t["weighted_tanb"] == 2.5, t
        assert t["gain"] == 200 and abs(t["gain_pct"] - 0.2) < 1e-9, t
        # série: em 2026-09-01 a reserva ainda vale 4000 (carry-forward) + investimento 1000
        by = {r["date"]: r for r in s["series"]}
        assert by["2026-09-01"]["total"] == 5000 and by["2026-10-01"]["total"] == 6200, by

        # upsert do mesmo dia atualiza em vez de duplicar
        c.execute("INSERT INTO asset_snapshots(asset_id, date, value, invested) VALUES (1,'2026-10-01',5100,NULL) "
                  "ON CONFLICT(asset_id, date) DO UPDATE SET value=excluded.value, invested=excluded.invested")
        assert assets.summary(c)["totals"]["reserve"] == 5100

        # apagar ativo apaga o histórico (cascade)
        c.execute("DELETE FROM assets WHERE id=2")
        assert c.execute("SELECT COUNT(*) n FROM asset_snapshots WHERE asset_id=2").fetchone()["n"] == 0

        # isolamento: ativos não afetam o património/KPIs das contas
        ingest.ingest_file(c, ROOT / "data" / "samples" / "extrato_abr_ago.xlsx", "Banco A")
        ingest.ingest_file(c, ROOT / "data" / "samples" / "cartao_junho.csv", "Banco B")
        rep = kpis.report(db.load_transactions(c), db.load_table(c, "categories"), db.load_table(c, "accounts"))
        names = {b["account"] for b in rep["balances"]}
        assert names == {"Banco A", "Banco B"}, names  # 2 bancos = 2 contas, sem ativos misturados
    print("ASSETS OK")


if __name__ == "__main__":
    main()
