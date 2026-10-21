"""Perguntar aos dados: LLM local escreve SQL, nós validamos e executamos só de leitura."""
import re
import sqlite3
from datetime import date

from . import db, llm

SCHEMA_DOC = """\
Base de dados SQLite. Usa preferencialmente a vista v_tx:

v_tx(id, date TEXT 'yyyy-mm-dd', month TEXT 'yyyy-mm', description, amount REAL, category,
     kind TEXT, bucket TEXT, fixed INT, account, account_type)

Regras de negócio:
- amount < 0 é saída de dinheiro; amount > 0 é entrada.
- kind: 'income' (rendimento), 'expense' (despesa), 'saving' (investimento/poupança), 'transfer' (transferência interna, ignorar em rendimento e despesa).
- Despesas de um período = -SUM(amount) WHERE kind='expense'.
- Rendimento = SUM(amount) WHERE kind='income' AND amount>0.
- Investido = -SUM(amount) WHERE kind='saving'.
- bucket: 'needs' (necessidades) ou 'wants' (desejos). fixed=1 são despesas fixas.
- Categorias possíveis incluem: Rendimento, Habitação, Casa & Utilidades, Seguros, Impostos & Seg. Social, Supermercado,
  Transportes, Saúde, Educação, Restauração, Lazer, Compras, Viagens, Subscrições, Fitness, Dinheiro, Investimento,
  Transferências internas, Sem categoria.
- Para meses usa a coluna month; para "últimos N meses" usa date >= date('now','-N months').
Outras tabelas: accounts(id, name, type, opening_balance) — uma conta por banco, categories(name, kind, bucket, fixed), imports(...).
Reserva e investimentos (fora dos KPIs do dia a dia): assets(id, name, institution, kind 'reserve'|'investment', tanb % ao ano bruta),
asset_snapshots(asset_id, date, value, invested) — o valor atual de um ativo é o snapshot com a data mais recente.
Só podes ler. Limita resultados a 100 linhas.
"""

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum|reindex|load_extension)\b", re.I)


def validate(sql: str) -> str:
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise ValueError("query vazia")
    if ";" in s:
        raise ValueError("só é permitida uma instrução")
    if not re.match(r"(?is)^(select|with)\b", s):
        raise ValueError("só são permitidas queries SELECT")
    if FORBIDDEN.search(s):
        raise ValueError("a query contém uma operação não permitida")
    return s


def run_readonly(sql: str, limit: int = 200):
    conn = sqlite3.connect(f"file:{db.db_path()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchmany(limit)]
        return cols, rows
    finally:
        conn.close()


def answer(question: str) -> dict:
    if not llm.enabled():
        return {"error": "LLM local desligado. Arranca com FIN_LLM=<modelo> (ver README)."}
    sql, err = None, None
    for _ in range(2):  # 1 tentativa + 1 correção com o erro
        sql = llm.generate_sql(question, SCHEMA_DOC, date.today().isoformat(), sql, err)
        if not sql:
            return {"error": "O modelo não respondeu. O Ollama está a correr?"}
        try:
            clean = validate(sql)
            cols, rows = run_readonly(clean)
            break
        except Exception as e:
            err = str(e)
    else:
        return {"error": f"Não consegui gerar uma query válida ({err}).", "sql": sql}
    text = llm.summarize(question, clean, cols, rows)
    return {"answer": text, "sql": clean, "columns": cols, "rows": rows}
