"""Simulação do saldo das contas à ordem: ritmo do histórico + itens planeados (por evento).

Modelo (propositadamente simples, para se poder explicar):
- saldo inicial = saldo líquido (contas 'liquid') à data do último movimento importado;
- base = médias da janela recente das contas 'liquid', SEM movimentos associados a eventos
  (esses são pontuais e entram pelos itens planeados, para não contar duas vezes):
    * rendimento: chega de uma vez, no dia do mês em que costuma chegar (mediana histórica);
    * despesas (+ investido, opcional): espalhadas por igual pelos dias;
- itens planeados: contam na sua data, ponderados pela certeza.

Horizonte: termina sempre num fim de mês. 0 = até ao fim deste mês (série diária);
N > 0 = até ao fim do N-ésimo mês seguinte (série mês a mês, um ponto por fim de mês).
"Este mês" é o de hoje, ou o do último movimento se for mais recente. O cálculo é sempre
diário por dentro, para o ponto mais baixo e a data abaixo do limite não se perderem entre pontos.
"""
import pandas as pd

from . import kpis

WEIGHTS = {"certain": 1.0, "likely": 0.7, "maybe": 0.3}
MODES = ("expected", "pessimistic")


def weight(certainty: str, amount: float, mode: str) -> float:
    if mode == "pessimistic":
        # despesas contam inteiras; entradas só se forem certas
        return 1.0 if (amount < 0 or certainty == "certain") else 0.0
    return WEIGHTS.get(certainty, 1.0)


def baseline(df: pd.DataFrame, window_days: int = 90, include_saving: bool = True) -> dict:
    end = df["date"].max()
    span = int((end - df["date"].min()).days) + 1
    days = max(1, min(window_days, span))
    liq = df[(df["account_type"] == "liquid") & df["event_id"].isna() & (df["date"] > end - pd.Timedelta(days=days))]
    months = days / 30.4375
    inc = liq[(liq["kind"] == "income") & (liq["amount"] > 0)]
    expenses = max(0.0, -liq.loc[liq["kind"] == "expense", "amount"].sum())
    invested = max(0.0, -liq.loc[liq["kind"] == "saving", "amount"].sum()) if include_saving else 0.0
    return {
        "window_days": days,
        "thin": span < 60,
        "monthly_income": inc["amount"].sum() / months,
        "income_day": int(inc["date"].dt.day.median()) if len(inc) else 1,
        "monthly_expenses": expenses / months,
        "monthly_invested": invested / months,
        "daily_outflow": (expenses + invested) / days,
    }


def run(tx, cats, accounts, planned, horizon_months=0, floor=0.0, mode="expected", include_saving=True, window_days=90, today=None) -> dict:
    if tx.empty:
        return {"empty": True}
    df = kpis._prepare(tx, cats)
    b = baseline(df, window_days, include_saving)

    # mesmo "líquido" dos KPIs: saldo inicial das contas à ordem + todos os seus movimentos (inclui os de eventos)
    start_balance = float(
        accounts.loc[accounts["type"] == "liquid", "opening_balance"].sum()
        + df.loc[df["account_type"] == "liquid", "amount"].sum()
    )

    start = df["date"].max().normalize()
    anchor = max(start, pd.Timestamp(today or pd.Timestamp.today()).normalize())
    end = anchor + pd.offsets.MonthEnd(0) + pd.offsets.MonthEnd(horizon_months)

    items, ignored = [], 0
    for p in planned:
        if not p.get("include_in_sim", 1):
            continue
        d = pd.Timestamp(p["date"])
        if d <= start:
            ignored += 1  # já está no saldo (ou devia ter sido importado)
            continue
        if d > end:
            continue
        w = weight(p["certainty"], p["amount"], mode)
        items.append({**p, "weight": w, "effective": p["amount"] * w})

    by_day: dict[pd.Timestamp, float] = {}
    for it in items:
        d = pd.Timestamp(it["date"])
        by_day[d] = by_day.get(d, 0.0) + it["effective"]

    dates, balance = [start], [start_balance]
    bal = start_balance
    for d in pd.date_range(start + pd.Timedelta(days=1), end):
        bal -= b["daily_outflow"]
        if d.day == min(b["income_day"], d.days_in_month):
            bal += b["monthly_income"]
        bal += by_day.get(d, 0.0)
        dates.append(d)
        balance.append(bal)

    low = min(range(len(balance)), key=balance.__getitem__)
    below = next((dates[i] for i, v in enumerate(balance) if v < floor), None)

    # série devolvida: diária até ao fim deste mês; mês a mês (início + cada fim de mês) para horizontes maiores
    step = "day" if horizon_months == 0 else "month"
    keep = [i for i, d in enumerate(dates) if step == "day" or i == 0 or d.is_month_end]
    # cada ponto marca se há itens planeados desde o ponto anterior
    has_item = [False] + [any(dates[a] < d <= dates[b] for d in by_day) for a, b in zip(keep, keep[1:])]

    impact: dict[str, float] = {}
    for it in items:
        key = it["event"] or "Avulsos"
        impact[key] = impact.get(key, 0.0) + it["effective"]

    return kpis._clean(
        {
            "empty": False,
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
            "start_balance": start_balance,
            "final_balance": balance[-1],
            "min_balance": balance[low],
            "min_date": dates[low].date().isoformat(),
            "floor": floor,
            "below_floor_date": below.date().isoformat() if below is not None else None,
            "mode": mode,
            "baseline": b,
            "planned_total": sum(it["effective"] for it in items),
            "ignored": ignored,
            "horizon_months": horizon_months,
            "step": step,
            "series": {
                "dates": [dates[i].date().isoformat() for i in keep],
                "balance": [round(balance[i], 2) for i in keep],
                "has_item": has_item,
            },
            "items": [
                {"date": it["date"], "description": it["description"], "event": it["event"], "category": it["category"],
                 "certainty": it["certainty"], "amount": it["amount"], "effective": it["effective"]}
                for it in items
            ],
            "impact": [{"event": k, "amount": v} for k, v in sorted(impact.items(), key=lambda kv: kv[1])],
        }
    )
