"""Categorização: regras por substring (determinístico) + LLM local como fallback."""
import re
import unicodedata

from . import llm


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


def match_rule(description: str, rules: list[tuple[str, str]]) -> str | None:
    d = " " + norm(description) + " "
    for pattern, category in rules:  # já ordenadas por comprimento desc
        if pattern in d:
            return category
    return None


def apply(conn) -> dict:
    """Categoriza tudo o que não é manual. Regras primeiro; LLM só para o que sobra."""
    rules = [
        (r["pattern"], r["category"])
        for r in conn.execute("SELECT pattern, category FROM rules ORDER BY LENGTH(pattern) DESC")
    ]
    rows = conn.execute("SELECT id, description, category FROM transactions WHERE cat_source != 'manual'").fetchall()
    by_rule = 0
    unmatched: dict[str, list[int]] = {}
    for row in rows:
        cat = match_rule(row["description"], rules)
        if cat:
            if cat != row["category"] or True:
                conn.execute("UPDATE transactions SET category=?, cat_source='rule' WHERE id=?", (cat, row["id"]))
            by_rule += 1
        elif row["category"] == "Sem categoria":
            unmatched.setdefault(row["description"], []).append(row["id"])

    by_llm = 0
    if llm.enabled() and unmatched:
        cats = [r["name"] for r in conn.execute("SELECT name FROM categories")]
        descs = list(unmatched)
        for i in range(0, len(descs), 40):
            for desc, cat in llm.suggest_categories(descs[i : i + 40], cats).items():
                if cat == "Sem categoria":
                    continue
                for tid in unmatched[desc]:
                    conn.execute("UPDATE transactions SET category=?, cat_source='llm' WHERE id=?", (cat, tid))
                    by_llm += 1
    return {"by_rule": by_rule, "by_llm": by_llm}
