"""Testa o 'Perguntar' com um LLM simulado: validação, só-leitura e correção de erro.  python tests/test_ask.py"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["FIN_DB"] = str(Path(tempfile.mkdtemp()) / "ask.db")
os.environ["FIN_LLM"] = "fake"

from fin import ask, db, ingest, llm  # noqa: E402


def main():
    db.init()
    with db.connect() as c:
        ingest.ingest_file(c, ROOT / "data" / "samples" / "extrato_abr_ago.xlsx", "Conta")

    # 1) SQL válida sobre a vista: despesas em Supermercado
    llm.generate_sql = lambda q, s, t, p=None, e=None: "SELECT month, -SUM(amount) AS total FROM v_tx WHERE kind='expense' AND category='Supermercado' GROUP BY month"
    llm.summarize = lambda *a: "ok"
    r = ask.answer("quanto gasto em supermercado por mês?")
    assert r["columns"] == ["month", "total"] and len(r["rows"]) == 5 and all(x[1] > 0 for x in r["rows"]), r

    # 2) tentativas de escrita são bloqueadas (duas vezes -> erro, sem alterar a BD)
    for bad in ["DELETE FROM transactions", "SELECT 1; DROP TABLE transactions", "WITH x AS (SELECT 1) UPDATE transactions SET amount=0", "PRAGMA writable_schema=1"]:
        llm.generate_sql = lambda q, s, t, p=None, e=None, b=bad: b
        r = ask.answer("x")
        assert "error" in r and "rows" not in r, (bad, r)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 100

    # 3) o modelo corrige a query depois do erro
    calls = []
    def flaky(q, s, t, prev=None, err=None):
        calls.append(err)
        return "SELECT nao_existe FROM v_tx" if err is None else "SELECT COUNT(*) AS n FROM v_tx"
    llm.generate_sql = flaky
    r = ask.answer("quantos movimentos?")
    assert r["rows"] == [[100]] and calls[0] is None and calls[1], (r, calls)

    # 4) a ligação é mesmo só de leitura (mesmo que a validação falhasse)
    try:
        ask.run_readonly("INSERT INTO accounts(name) VALUES ('x')")
        raise AssertionError("escrita deveria falhar")
    except Exception as e:
        assert "readonly" in str(e).lower() or "read-only" in str(e).lower() or "query_only" in str(e).lower() or "attempt to write" in str(e).lower(), e
    print("ASK OK")


if __name__ == "__main__":
    main()
