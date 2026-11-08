"""Categorias próprias: criar, editar e apagar. As de origem (SEED_CATEGORIES) editam-se mas não se apagam."""
KINDS = ("income", "expense", "saving", "transfer")
BUCKETS = ("needs", "wants", "other")
FALLBACK = "Sem categoria"


def _check(kind: str, bucket: str):
    if kind not in KINDS:
        raise ValueError("kind deve ser income, expense, saving ou transfer")
    if bucket not in BUCKETS:
        raise ValueError("bucket deve ser needs, wants ou other")


def create(conn, name: str, kind: str = "expense", bucket: str = "other", fixed: bool = False) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("A categoria precisa de nome")
    _check(kind, bucket)
    if conn.execute("SELECT 1 FROM categories WHERE lower(name) = lower(?)", (name,)).fetchone():
        raise FileExistsError("Já existe uma categoria com esse nome")
    conn.execute(
        "INSERT INTO categories(name, kind, bucket, fixed, custom) VALUES (?,?,?,?,1)", (name, kind, bucket, int(fixed))
    )
    return name


def update(conn, name: str, kind: str, bucket: str, fixed: bool, budget: float | None = None):
    _check(kind, bucket)
    if name == FALLBACK:
        raise ValueError(f"'{FALLBACK}' não se edita")
    if budget is not None and budget < 0:
        raise ValueError("O orçamento não pode ser negativo")
    budget = budget or None  # 0 = sem orçamento
    cur = conn.execute(
        "UPDATE categories SET kind=?, bucket=?, fixed=?, budget=? WHERE name=?", (kind, bucket, int(fixed), budget, name)
    )
    if not cur.rowcount:
        raise LookupError("Categoria inexistente")


def delete(conn, name: str) -> int:
    """Apaga uma categoria própria. Os movimentos passam a 'Sem categoria'; as regras dela saem."""
    row = conn.execute("SELECT custom FROM categories WHERE name=?", (name,)).fetchone()
    if not row:
        raise LookupError("Categoria inexistente")
    if not row["custom"]:
        raise PermissionError("Só se podem apagar categorias criadas por ti")
    moved = conn.execute(
        "UPDATE transactions SET category=?, cat_source='none' WHERE category=?", (FALLBACK, name)
    ).rowcount
    conn.execute("DELETE FROM rules WHERE category=?", (name,))
    conn.execute("UPDATE planned_items SET category=NULL WHERE category=?", (name,))
    conn.execute("DELETE FROM categories WHERE name=?", (name,))
    return moved
