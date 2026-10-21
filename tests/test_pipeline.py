"""Teste end-to-end: ingestão (Excel, CSV, PDF texto, PDF OCR) -> BD -> KPIs.  python tests/test_pipeline.py"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["FIN_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ.pop("FIN_LLM", None)

from fin import db, ingest, kpis  # noqa: E402

SAMPLES = ROOT / "data" / "samples"


def main():
    db.init()
    with db.connect() as c:
        res = {}
        for f, acct in [
            ("extrato_abr_ago.xlsx", "Conta"), ("cartao_junho.csv", "Cartão"),
            ("extrato_setembro_texto.pdf", "Conta"), ("extrato_outubro_scan.pdf", "Conta"),
        ]:
            res[f] = ingest.ingest_file(c, SAMPLES / f, acct)
            print(f, res[f])
        assert res["extrato_abr_ago.xlsx"]["method"] == "excel" and res["extrato_abr_ago.xlsx"]["rows_new"] > 80
        assert res["cartao_junho.csv"]["rows_new"] == 3
        assert res["extrato_setembro_texto.pdf"]["method"] == "pdf-text"
        assert res["extrato_outubro_scan.pdf"]["method"] == "ocr", "PDF digitalizado deve passar por OCR"
        assert res["extrato_outubro_scan.pdf"]["rows_new"] == 6

        # re-importar não duplica
        again = ingest.ingest_file(c, SAMPLES / "extrato_outubro_scan.pdf", "Conta")
        assert again["status"] == "duplicate"

        # sinais do PDF de texto vieram do saldo: renda negativa, salário/fatura positivo
        rows = {r["description"]: r["amount"] for r in c.execute("SELECT description, amount FROM transactions WHERE date LIKE '2026-09%'")}
        assert rows["RENDA APARTAMENTO"] < 0 and rows["PAGAMENTO SERVICOS FATURA RECIBO"] > 0
        # OCR: salário positivo
        sal = c.execute("SELECT amount, category FROM transactions WHERE description LIKE 'SALARIO%'").fetchone()
        assert sal["amount"] == 2150.0 and sal["category"] == "Rendimento", dict(sal)

        tx, cats, acc = db.load_transactions(c), db.load_table(c, "categories"), db.load_table(c, "accounts")
        rep = kpis.report(tx, cats, acc, tax_pct=0.25)
        k = rep["kpi"]
        print({x: k[x] for x in ("income", "expenses", "net", "savings_rate", "burn_3m", "runway_months", "fixed_pct_income", "recurring_annual", "uncategorized")})
        assert rep["month"] == "2026-10" and abs(k["net"] - (k["income"] - k["expenses"])) < 0.01
        assert abs(k["tax_reserve_month"] - 0.25 * k["income"]) < 0.01
        assert any("NETFLIX" in r["name"].upper() for r in rep["recurring"])
        assert len(rep["monthly"]) == 7
        # quase tudo categorizado por regras
        unc = c.execute("SELECT COUNT(*) n FROM transactions WHERE category='Sem categoria'").fetchone()["n"]
        total = c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
        assert unc / total < 0.1, (unc, total)
    print("TUDO OK")


if __name__ == "__main__":
    main()
