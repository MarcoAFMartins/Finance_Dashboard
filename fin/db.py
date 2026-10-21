"""SQLite: esquema, seeds e helpers."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> Path:
    return Path(os.environ.get("FIN_DB", ROOT / "data" / "finance.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL DEFAULT 'liquid',          -- liquid | savings | investment
    opening_balance REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories(
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                           -- income | expense | saving | transfer
    bucket TEXT NOT NULL DEFAULT 'other',         -- needs | wants | other
    fixed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY,
    pattern TEXT UNIQUE NOT NULL,                 -- substring normalizada (sem acentos, minúsculas)
    category TEXT NOT NULL REFERENCES categories(name)
);
CREATE TABLE IF NOT EXISTS imports(
    id INTEGER PRIMARY KEY,
    filename TEXT,
    file_hash TEXT NOT NULL,
    method TEXT,                                  -- excel | csv | pdf-text | ocr | mixed | llm
    account TEXT,
    rows_found INTEGER,
    rows_new INTEGER,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_hash, account)
);
CREATE TABLE IF NOT EXISTS transactions(
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE NOT NULL,                     -- hash estável, evita duplicados em re-importações
    date TEXT NOT NULL,                           -- ISO yyyy-mm-dd
    description TEXT NOT NULL,
    amount REAL NOT NULL,                         -- negativo = saída
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    category TEXT NOT NULL DEFAULT 'Sem categoria',
    cat_source TEXT NOT NULL DEFAULT 'none',      -- none | rule | llm | manual
    import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE
);
-- Reserva e investimentos: separados das contas e dos KPIs do dia a dia
CREATE TABLE IF NOT EXISTS assets(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    institution TEXT,
    kind TEXT NOT NULL DEFAULT 'reserve',         -- reserve | investment
    tanb REAL,                                    -- % ao ano, bruta (reservas/depósitos)
    note TEXT
);
CREATE TABLE IF NOT EXISTS asset_snapshots(
    id INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    value REAL NOT NULL,                          -- valor atual
    invested REAL,                                -- capital aplicado (opcional, para investimentos)
    UNIQUE(asset_id, date)
);

-- vista usada pelo "Perguntar": mesma lógica de kind dos KPIs, sem JOINs para o LLM errar
CREATE VIEW IF NOT EXISTS v_tx AS
SELECT t.id, t.date, substr(t.date,1,7) AS month, t.description, t.amount, t.category,
       CASE WHEN t.category='Sem categoria' THEN (CASE WHEN t.amount>0 THEN 'income' ELSE 'expense' END)
            ELSE COALESCE(c.kind,'expense') END AS kind,
       c.bucket, c.fixed, a.name AS account, a.type AS account_type
FROM transactions t
JOIN accounts a ON a.id = t.account_id
LEFT JOIN categories c ON c.name = t.category;
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_cat ON transactions(category);
"""

# (name, kind, bucket, fixed)
SEED_CATEGORIES = [
    ("Rendimento", "income", "other", 0),
    ("Outros rendimentos", "income", "other", 0),
    ("Habitação", "expense", "needs", 1),
    ("Casa & Utilidades", "expense", "needs", 1),
    ("Seguros", "expense", "needs", 1),
    ("Impostos & Seg. Social", "expense", "needs", 1),
    ("Supermercado", "expense", "needs", 0),
    ("Transportes", "expense", "needs", 0),
    ("Saúde", "expense", "needs", 0),
    ("Educação", "expense", "needs", 0),
    ("Restauração", "expense", "wants", 0),
    ("Lazer", "expense", "wants", 0),
    ("Compras", "expense", "wants", 0),
    ("Viagens", "expense", "wants", 0),
    ("Subscrições", "expense", "wants", 1),
    ("Fitness", "expense", "wants", 1),
    ("Dinheiro", "expense", "wants", 0),
    ("Investimento", "saving", "other", 0),
    ("Transferências internas", "transfer", "other", 0),
    ("Sem categoria", "expense", "other", 0),
]

SEED_RULES = {
    "Rendimento": ["salario", "vencimento", "fatura recibo", "recibo verde", "pagamento servicos"],
    "Habitação": ["renda", "condominio", "senhorio"],
    "Casa & Utilidades": ["edp", "endesa", "iberdrola", "epal", "vodafone", "meo ", "nos comunic", "nowo", "ikea", "leroy"],
    "Seguros": ["seguro", "fidelidade", "ageas", "allianz"],
    "Impostos & Seg. Social": ["seguranca social", "autoridade tributaria", "irs", "iva "],
    "Supermercado": ["continente", "pingo doce", "lidl", "auchan", "minipreco", "mercadona", "intermarche", "aldi"],
    "Transportes": ["uber ", "bolt", "via verde", "galp", "repsol", "bp ", "cp comboios", "metro", "carris", "navegante", "fertagus", "cabify"],
    "Saúde": ["farmacia", "hospital", "clinica", "dentista", "cuf ", "lusiadas", "wells"],
    "Restauração": ["uber eats", "glovo", "mcdonald", "burger king", "restaurante", "cafe ", "padaria", "starbucks", "telepizza", "bolt food"],
    "Lazer": ["cinema", "ticketline", "bilheteira", "steam", "playstation", "bar "],
    "Compras": ["amazon", "worten", "fnac", "zara", "h&m", "aliexpress", "pcdiga", "decathlon", "el corte"],
    "Viagens": ["ryanair", "easyjet", "tap air", "booking", "airbnb", "hotel", "flixbus", "trivago"],
    "Subscrições": ["netflix", "spotify", "hbo", "disney", "youtube", "apple.com/bill", "google one", "openai", "anthropic", "notion", "github", "icloud", "adobe"],
    "Fitness": ["ginasio", "holmes place", "fitness hut", "solinca", "gym"],
    "Dinheiro": ["levantamento", "atm", "multibanco lev"],
    "Investimento": ["trading212", "trading 212", "xtb", "degiro", "interactive brokers", "etf", "bolsa", "ppr"],
    "Transferências internas": ["transferencia propria", "entre contas", "poupanca"],
}


@contextmanager
def connect():
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    from .categorize import norm

    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO categories(name, kind, bucket, fixed) VALUES (?,?,?,?)",
            SEED_CATEGORIES,
        )
        for cat, pats in SEED_RULES.items():
            conn.executemany(
                "INSERT OR IGNORE INTO rules(pattern, category) VALUES (?,?)",
                [(norm(p), cat) for p in pats],
            )


def get_or_create_account(conn, name: str, type_: str = "liquid") -> int:
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO accounts(name, type) VALUES (?,?)", (name, type_))
    return cur.lastrowid


def load_transactions(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT t.id, t.date, t.description, t.amount, t.category, t.cat_source,
                  a.name AS account, a.type AS account_type
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           ORDER BY t.date DESC, t.id DESC""",
        conn,
    )
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_table(conn, name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {name}", conn)
