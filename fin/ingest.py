"""Importação: Excel/CSV, PDF com texto, PDF digitalizado / imagem (OCR), com fallback LLM."""
import hashlib
import re
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd

from . import categorize, db, llm, ocr

# ---------- parsing de números e datas ----------

NUM_RE = re.compile(r"(?<![\w/])[-+]?\(?\d[\d.,]*[.,]\d{2}\)?-?(?![\d/])")
DATE_TOKEN = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}[-/.]\d{1,2})"
LINE_RE = re.compile(rf"^\s*{DATE_TOKEN}(?:\s+{DATE_TOKEN})?\s+(.+)$")
INCOME_WORDS = ("salario", "vencimento", "recebid", "credito", "deposito", "reembolso", "transferencia de", "recibo", "fatura")


def parse_amount(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace("€", "").replace("EUR", "").replace(" ", "").replace(" ", "")
    neg = t.startswith("-") or t.endswith("-") or (t.startswith("(") and t.endswith(")"))
    t = t.strip("-+()")
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        thou = "." if dec == "," else ","
        t = t.replace(thou, "").replace(dec, ".")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) else t.replace(",", "")
    elif "." in t and not re.search(r"\.\d{1,2}$", t):
        t = t.replace(".", "")
    v = float(t)
    return -v if neg else v


def parse_date(tok: str, year_hint: int) -> str | None:
    tok = tok.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
            return date.fromisoformat(tok).isoformat()
        parts = re.split(r"[-/.]", tok)
        d, m = int(parts[0]), int(parts[1])
        y = int(parts[2]) if len(parts) == 3 else year_hint
        if y < 100:
            y += 2000
        return date(y, m, d).isoformat()
    except (ValueError, IndexError):
        return None


# ---------- Excel / CSV ----------

ALIASES = {
    "date": ["data", "data mov", "data movimento", "data operacao", "data valor", "date", "booking date", "started date"],
    "description": ["descricao", "descritivo", "descriptivo", "movimento", "description", "detalhes", "referencia", "merchant"],
    "amount": ["montante", "valor", "amount", "importe", "quantia"],
    "debit": ["debito", "debit", "saidas", "despesa", "levantamentos"],
    "credit": ["credito", "credit", "entradas", "receita", "depositos"],
}


def _n(s) -> str:
    return categorize.norm(s).replace("(", "").replace(")", "").replace(".", "").strip()


def _map_columns(cols) -> dict:
    mapping = {}
    for c in cols:
        for key, names in ALIASES.items():
            if key not in mapping and _n(c) in names:
                mapping[key] = c
    return mapping


def _find_header(raw: pd.DataFrame) -> int | None:
    for i in range(min(25, len(raw))):
        m = _map_columns(raw.iloc[i].dropna().tolist())
        if "date" in m and "description" in m and ("amount" in m or "debit" in m or "credit" in m):
            return i
    return None


def _read_tabular(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".csv", ".txt"):
        raw = pd.read_csv(path, sep=None, engine="python", header=None, dtype=str, encoding_errors="replace")
        return _frame_from_raw(raw)
    frames = []
    for _, raw in pd.read_excel(path, sheet_name=None, header=None, dtype=object).items():
        try:
            frames.append(_frame_from_raw(raw))
        except ValueError:
            continue
    if not frames:
        raise ValueError("Nenhuma folha com colunas reconhecidas (data, descrição, montante/débito/crédito).")
    return pd.concat(frames, ignore_index=True)


def _frame_from_raw(raw: pd.DataFrame) -> pd.DataFrame:
    h = _find_header(raw)
    if h is None:
        raise ValueError("Não encontrei cabeçalho com data, descrição e montante (ou débito/crédito).")
    df = raw.iloc[h + 1 :].copy().reset_index(drop=True)
    df.columns = [str(c) if pd.notna(c) else f"_c{i}" for i, c in enumerate(raw.iloc[h])]
    m = _map_columns(df.columns)
    out = pd.DataFrame()
    d = df[m["date"]]
    out["date"] = [
        v.date().isoformat() if hasattr(v, "date") and not isinstance(v, str) else parse_date(str(v), date.today().year)
        for v in d
    ]
    out["description"] = df[m["description"]].astype(str).str.strip()
    if "amount" in m:
        out["amount"] = [parse_amount(v) if pd.notna(v) and str(v).strip() else None for v in df[m["amount"]]]
    else:
        deb = [parse_amount(v) if pd.notna(v) and str(v).strip() else 0.0 for v in df[m.get("debit", df.columns[0])]] if "debit" in m else [0.0] * len(df)
        cre = [parse_amount(v) if pd.notna(v) and str(v).strip() else 0.0 for v in df[m["credit"]]] if "credit" in m else [0.0] * len(df)
        out["amount"] = [c - abs(x) for x, c in zip(deb, cre)]
    out = out.dropna(subset=["date", "amount"])
    out = out[(out["description"] != "") & (out["description"].str.lower() != "nan")]
    return out.reset_index(drop=True)


# ---------- PDF / OCR ----------


def _year_hint(text: str) -> int:
    years = re.findall(r"\b(20\d{2})\b", text)
    return int(Counter(years).most_common(1)[0][0]) if years else date.today().year


def _parse_lines(pages: list[str]) -> list[dict]:
    full = "\n".join(pages)
    year = _year_hint(full)
    rows = []
    for line in full.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        d1, _d2, rest = m.groups()
        iso = parse_date(d1, year)
        nums = list(NUM_RE.finditer(rest))
        if not iso or not nums:
            continue
        desc = rest[: nums[0].start()].strip(" -–|")
        if not desc:
            continue
        amount_tok = nums[-2] if len(nums) >= 2 else nums[-1]
        balance = parse_amount(nums[-1].group()) if len(nums) >= 2 else None
        rows.append(
            {"date": iso, "description": desc, "amount": parse_amount(amount_tok.group()), "balance": balance,
             "signed": bool(re.search(r"[-(]|-$", amount_tok.group()))}
        )
    return _resolve_signs(rows)


def _resolve_signs(rows: list[dict]) -> list[dict]:
    """Extratos sem sinal: usa a variação do saldo; senão palavras-chave."""
    if not rows:
        return rows
    descending = rows[0]["date"] > rows[-1]["date"]
    seq = list(reversed(rows)) if descending else rows
    prev = None
    for r in seq:
        amt = r["amount"]
        if r["balance"] is not None and prev is not None:
            delta = round(r["balance"] - prev, 2)
            if abs(abs(delta) - abs(amt)) < 0.011:
                amt = delta
        elif not r["signed"]:
            amt = abs(amt) if any(w in categorize.norm(r["description"]) for w in INCOME_WORDS) else -abs(amt)
        r["amount"] = amt
        if r["balance"] is not None:
            prev = r["balance"]
    return rows


def _read_pdf_like(path: Path) -> tuple[pd.DataFrame, str]:
    pages, method = ocr.image_text(path) if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff") else ocr.pdf_pages_text(path)
    rows = _parse_lines(pages)
    if not rows and llm.enabled():
        rows = llm.parse_statement("\n".join(pages), _year_hint("\n".join(pages)))
        method = f"{method}+llm"
    if not rows:
        raise ValueError("Não consegui extrair movimentos deste documento (tenta Excel/CSV ou ativa o LLM local).")
    return pd.DataFrame(rows)[["date", "description", "amount"]], method


# ---------- pipeline ----------


def ingest_file(conn, path: Path, account: str, account_type: str = "liquid", display_name: str | None = None) -> dict:
    name = display_name or path.name
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if conn.execute("SELECT 1 FROM imports WHERE file_hash=? AND account=?", (file_hash, account)).fetchone():
        return {"file": name, "status": "duplicate", "rows_found": 0, "rows_new": 0}

    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls", ".csv", ".txt"):
        df, method = _read_tabular(path), ext.lstrip(".").replace("xlsm", "excel").replace("xlsx", "excel").replace("xls", "excel")
    elif ext == ".pdf" or ext in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"):
        df, method = _read_pdf_like(path)
    else:
        raise ValueError(f"Formato não suportado: {ext}")

    account_id = db.get_or_create_account(conn, account, account_type)
    cur = conn.execute(
        "INSERT INTO imports(filename, file_hash, method, account, rows_found, rows_new) VALUES (?,?,?,?,?,0)",
        (name, file_hash, method, account, len(df)),
    )
    import_id = cur.lastrowid

    seen: Counter = Counter()
    new = 0
    for r in df.itertuples(index=False):
        key = f"{account}|{r.date}|{categorize.norm(r.description)}|{r.amount:.2f}"
        seen[key] += 1  # permite movimentos genuinamente repetidos no mesmo ficheiro
        uid = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()
        cur = conn.execute(
            "INSERT OR IGNORE INTO transactions(uid, date, description, amount, account_id, import_id) VALUES (?,?,?,?,?,?)",
            (uid, r.date, r.description, float(r.amount), account_id, import_id),
        )
        new += cur.rowcount
    conn.execute("UPDATE imports SET rows_new=? WHERE id=?", (new, import_id))
    cats = categorize.apply(conn)
    return {"file": name, "status": "ok", "method": method, "rows_found": len(df), "rows_new": new, **cats}
