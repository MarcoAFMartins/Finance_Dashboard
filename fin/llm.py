"""LLM local opcional (Ollama). Desligado por defeito.

Ativar:  FIN_LLM=qwen2.5:7b-instruct  (e `ollama serve` a correr)
Opcional: FIN_LLM_URL=http://localhost:11434

Usado apenas como fallback:
  1. categorizar descrições que nenhuma regra apanha;
  2. extrair transações de texto de PDF/OCR quando o parser por regex não encontra nada.
Nada sai da máquina.
"""
import json
import os
import urllib.request


def model() -> str | None:
    return os.environ.get("FIN_LLM") or None


def enabled() -> bool:
    return model() is not None


def _generate(prompt: str, timeout: int = 180, as_json: bool = True):
    url = os.environ.get("FIN_LLM_URL", "http://localhost:11434").rstrip("/") + "/api/generate"
    payload = {"model": model(), "prompt": prompt, "stream": False, "options": {"temperature": 0}}
    if as_json:
        payload["format"] = "json"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = json.loads(r.read())["response"]
        return json.loads(text) if as_json else text.strip()
    except Exception:
        return None


def generate_sql(question: str, schema_doc: str, today: str, previous_sql: str | None = None, error: str | None = None) -> str | None:
    """Pede uma única query SELECT em SQLite. Se houve erro, devolve-o ao modelo para corrigir."""
    fix = f"\nA query anterior falhou.\nQuery: {previous_sql}\nErro: {error}\nCorrige-a.\n" if error else ""
    prompt = (
        "És um assistente que escreve SQL para SQLite sobre finanças pessoais.\n"
        f"{schema_doc}\nHoje é {today}.{fix}\n"
        "Escreve UMA única query SELECT (sem ponto e vírgula a meio) que responda à pergunta. "
        'Responde só com JSON: {"sql": "..."}\n'
        f"Pergunta: {question}"
    )
    data = _generate(prompt)
    return data.get("sql") if isinstance(data, dict) else None


def summarize(question: str, sql: str, columns: list[str], rows: list[list]) -> str | None:
    prompt = (
        "Responde em português europeu, em 1 a 3 frases curtas, à pergunta, usando APENAS os dados abaixo. "
        "Não inventes números; valores em euros. Se o resultado estiver vazio, diz que não há dados.\n"
        f"Pergunta: {question}\nColunas: {columns}\nLinhas (máx. 40): {json.dumps(rows[:40], ensure_ascii=False)}"
    )
    return _generate(prompt, as_json=False)


def suggest_categories(descriptions: list[str], categories: list[str]) -> dict[str, str]:
    """Devolve {descrição: categoria} só para respostas válidas."""
    if not enabled() or not descriptions:
        return {}
    prompt = (
        "Classifica cada descrição de movimento bancário português numa destas categorias: "
        f"{json.dumps(categories, ensure_ascii=False)}.\n"
        "Se não tiveres confiança, usa \"Sem categoria\".\n"
        'Responde só com JSON: {"resultados": [{"descricao": "...", "categoria": "..."}]}\n'
        f"Descrições: {json.dumps(descriptions, ensure_ascii=False)}"
    )
    data = _generate(prompt)
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for item in data.get("resultados", []):
            d, c = item.get("descricao"), item.get("categoria")
            if d in descriptions and c in categories:
                out[d] = c
    return out


def parse_statement(text: str, year_hint: int) -> list[dict]:
    """Extrai [{date, description, amount}] de texto de extrato mal estruturado."""
    if not enabled() or not text.strip():
        return []
    prompt = (
        "Extrai os movimentos bancários deste extrato (texto de OCR, pode ter erros). "
        f"Ano por defeito: {year_hint}. Datas em ISO (yyyy-mm-dd). "
        "amount é negativo para saídas/débitos e positivo para entradas/créditos. "
        "Ignora cabeçalhos, saldos e totais. "
        'Responde só com JSON: {"movimentos": [{"date": "...", "description": "...", "amount": 0.0}]}\n\n'
        f"{text[:12000]}"
    )
    data = _generate(prompt, timeout=300)
    rows = []
    if isinstance(data, dict):
        for m in data.get("movimentos", []):
            try:
                rows.append(
                    {"date": str(m["date"])[:10], "description": str(m["description"]).strip(), "amount": float(m["amount"])}
                )
            except (KeyError, TypeError, ValueError):
                continue
    return rows
