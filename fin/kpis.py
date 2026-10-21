"""KPIs de finanças pessoais a partir das transações."""
import re

import numpy as np
import pandas as pd

from . import categorize


def _prepare(tx: pd.DataFrame, cats: pd.DataFrame) -> pd.DataFrame:
    df = tx.merge(cats[["name", "kind", "bucket", "fixed"]], left_on="category", right_on="name", how="left")
    df["kind"] = df["kind"].fillna("expense")
    unc = df["category"] == "Sem categoria"
    df.loc[unc, "kind"] = np.where(df.loc[unc, "amount"] > 0, "income", "expense")
    df["month"] = df["date"].dt.strftime("%Y-%m")
    return df


def monthly_table(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("month")
    t = pd.DataFrame(
        {
            "income": g.apply(lambda x: x.loc[(x.kind == "income") & (x.amount > 0), "amount"].sum(), include_groups=False),
            "expenses": g.apply(lambda x: -x.loc[x.kind == "expense", "amount"].sum(), include_groups=False),
            "invested": g.apply(lambda x: -x.loc[x.kind == "saving", "amount"].sum(), include_groups=False),
        }
    )
    t["net"] = t["income"] - t["expenses"]
    t["savings_rate"] = np.where(t["income"] > 0, t["net"] / t["income"], np.nan)
    return t.sort_index()


def _recurring(df: pd.DataFrame) -> list[dict]:
    e = df[(df.kind == "expense") & (df.amount < 0)].copy()
    e["key"] = e["description"].map(lambda s: re.sub(r"\d+", "", categorize.norm(s)).strip())
    out = []
    for key, g in e.groupby("key"):
        months = g["month"].nunique()
        if not key or months < 3:
            continue
        amts = -g["amount"]
        if amts.std(ddof=0) / amts.mean() > 0.15:
            continue
        monthly = amts.groupby(g["month"]).sum().mean()
        out.append({"name": g["description"].iloc[0], "category": g["category"].iloc[0], "monthly": monthly, "annual": monthly * 12, "months": int(months)})
    return sorted(out, key=lambda r: -r["annual"])


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (o is None or np.isnan(o) or np.isinf(o)) else round(float(o), 4)
    if isinstance(o, (np.integer,)):
        return int(o)
    return o


def report(tx: pd.DataFrame, cats: pd.DataFrame, accounts: pd.DataFrame, month: str | None = None, tax_pct: float = 0.25) -> dict:
    if tx.empty:
        return {"empty": True}
    df = _prepare(tx, cats)
    mt = monthly_table(df)
    months = list(mt.index)
    month = month if month in months else months[-1]
    i = months.index(month)
    cur, prev = mt.loc[month], (mt.iloc[i - 1] if i > 0 else None)
    last3 = mt.iloc[max(0, i - 2) : i + 1]
    last6 = mt.iloc[max(0, i - 5) : i + 1]

    # saldos / net worth
    opening = accounts.set_index("name")["opening_balance"].to_dict()
    atype = accounts.set_index("name")["type"].to_dict()
    flows = df.groupby("account")["amount"].sum().to_dict()
    balances = {a: opening.get(a, 0) + flows.get(a, 0) for a in atype}
    liquid = sum(v for a, v in balances.items() if atype[a] == "liquid")
    net_worth = sum(balances.values())
    per_month = df.groupby("month")["amount"].sum().reindex(months, fill_value=0).cumsum()
    nw_series = (per_month + sum(opening.values())).to_dict()

    m = df[df.month == month]
    exp = m[m.kind == "expense"]
    burn3 = last3["expenses"].mean()
    inc = cur["income"]
    fixed = -exp.loc[exp.fixed == 1, "amount"].sum()
    needs = -exp.loc[exp.bucket == "needs", "amount"].sum()
    wants = -exp.loc[exp.bucket == "wants", "amount"].sum()
    ytd_income = mt.loc[[x for x in months if x[:4] == month[:4] and x <= month], "income"].sum()

    top = (-exp.groupby("category")["amount"].sum()).sort_values(ascending=False)
    top_cats = [{"category": k, "amount": v, "share": v / cur["expenses"] if cur["expenses"] else 0} for k, v in top.items() if v > 0][:10]
    trend_cats = list(top.index[:5])
    trend = {
        c: [float(-df[(df.month == mm) & (df.category == c) & (df.kind == "expense")]["amount"].sum()) for mm in last6.index]
        for c in trend_cats
    }
    big = m[(m.kind == "expense") & (m.amount < 0)].nsmallest(5, "amount")
    recurring = _recurring(df)

    cv = last6["income"].std(ddof=0) / last6["income"].mean() if len(last6) >= 3 and last6["income"].mean() > 0 else np.nan

    return _clean(
        {
            "empty": False,
            "month": month,
            "months": months,
            "kpi": {
                "income": inc,
                "expenses": cur["expenses"],
                "net": cur["net"],
                "invested": cur["invested"],
                "savings_rate": cur["savings_rate"],
                "savings_rate_3m": (last3["net"].sum() / last3["income"].sum()) if last3["income"].sum() > 0 else np.nan,
                "d_income": None if prev is None else cur["income"] - prev["income"],
                "d_expenses": None if prev is None else cur["expenses"] - prev["expenses"],
                "burn_3m": burn3,
                "runway_months": liquid / burn3 if burn3 > 0 else np.nan,
                "liquid": liquid,
                "net_worth": net_worth,
                "fixed_pct_income": fixed / inc if inc > 0 else np.nan,
                "fixed_pct_expenses": fixed / cur["expenses"] if cur["expenses"] > 0 else np.nan,
                "needs_pct": needs / inc if inc > 0 else np.nan,
                "wants_pct": wants / inc if inc > 0 else np.nan,
                "save_pct": cur["net"] / inc if inc > 0 else np.nan,
                "income_cv_6m": cv,
                "tax_reserve_month": inc * tax_pct,
                "tax_reserve_ytd": ytd_income * tax_pct,
                "recurring_annual": sum(r["annual"] for r in recurring),
                "uncategorized": int((m.category == "Sem categoria").sum()),
            },
            "monthly": [{"month": k, **{c: v[c] for c in ("income", "expenses", "invested", "net", "savings_rate")}} for k, v in mt.iterrows()],
            "net_worth_series": [{"month": k, "value": v} for k, v in nw_series.items()],
            "top_categories": top_cats,
            "category_trend": {"months": list(last6.index), "series": trend},
            "biggest": [{"date": r.date.date().isoformat(), "description": r.description, "amount": -r.amount, "category": r.category} for r in big.itertuples()],
            "recurring": recurring[:10],
            "balances": [{"account": a, "type": atype[a], "balance": b} for a, b in balances.items()],
        }
    )
