"""Eventos: agrupam despesas de categorias diferentes (ex.: viagem) e guardam itens planeados."""
from datetime import date

STATUSES = ("planned", "active", "done")
CERTAINTIES = ("certain", "likely", "maybe")


def check_iso(d: str | None) -> str | None:
    if d in (None, ""):
        return None
    date.fromisoformat(d)  # ValueError se inválida
    return d


def _planned(conn, event_id):
    rows = conn.execute(
        "SELECT * FROM planned_items WHERE event_id IS ? ORDER BY date, id", (event_id,)
    )
    return [dict(r) for r in rows]


def list_events(conn, month: str | None = None) -> dict:
    """Eventos com gasto real (por categoria), itens planeados e totais; mais os itens avulsos.

    Com month (yyyy-mm), cada evento traz também spent_month: o gasto desse mês.
    """
    events = [
        dict(r)
        for r in conn.execute("SELECT * FROM events ORDER BY COALESCE(start_date, '9999-12-31'), name")
    ]
    for e in events:
        rows = conn.execute(
            "SELECT category, -SUM(amount) AS amount, COUNT(*) AS n FROM v_tx "
            "WHERE event_id = ? GROUP BY category ORDER BY amount DESC",
            (e["id"],),
        ).fetchall()
        e["by_category"] = [{"category": r["category"], "amount": r["amount"]} for r in rows]
        e["spent"] = sum(r["amount"] for r in rows)  # líquido: reembolsos abatem
        e["tx_count"] = sum(r["n"] for r in rows)
        e["planned"] = _planned(conn, e["id"])
        e["planned_total"] = -sum(p["amount"] for p in e["planned"])
        e["include_in_sim"] = bool(e["include_in_sim"])
        if month:
            e["spent_month"] = conn.execute(
                "SELECT COALESCE(-SUM(amount), 0) FROM v_tx WHERE event_id = ? AND month = ?", (e["id"], month)
            ).fetchone()[0]
    return {"events": events, "loose": _planned(conn, None)}


def suggestions(conn, event_id: int) -> list[dict]:
    """Despesas sem evento dentro do intervalo de datas do evento. Só sugere; quem confirma é o utilizador."""
    ev = conn.execute("SELECT start_date, end_date FROM events WHERE id = ?", (event_id,)).fetchone()
    if not ev or not ev["start_date"] or not ev["end_date"]:
        return []
    rows = conn.execute(
        "SELECT id, date, description, amount, category, account FROM v_tx "
        "WHERE event_id IS NULL AND kind = 'expense' AND amount < 0 AND date BETWEEN ? AND ? "
        "ORDER BY date, id",
        (ev["start_date"], ev["end_date"]),
    )
    return [dict(r) for r in rows]


def assign(conn, tx_ids: list[int], event_id: int | None) -> int:
    """Associa movimentos a um evento (event_id None = remover do evento)."""
    if not tx_ids:
        return 0
    if event_id is not None and not conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
        raise LookupError("Evento inexistente")
    marks = ",".join("?" * len(tx_ids))
    cur = conn.execute(f"UPDATE transactions SET event_id = ? WHERE id IN ({marks})", [event_id, *tx_ids])
    return cur.rowcount


def planned_for_sim(conn) -> list[dict]:
    """Itens planeados + estado do evento (include_in_sim). Avulsos entram sempre."""
    rows = conn.execute(
        "SELECT p.id, p.event_id, e.name AS event, p.description, p.amount, p.date, p.category, p.certainty, "
        "COALESCE(e.include_in_sim, 1) AS include_in_sim "
        "FROM planned_items p LEFT JOIN events e ON e.id = p.event_id ORDER BY p.date, p.id"
    )
    return [dict(r) for r in rows]
