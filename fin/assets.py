"""Reserva e investimentos: valores por data (snapshots), TANB e totais. Fora dos KPIs do dia a dia."""


def summary(conn) -> dict:
    assets = [dict(r) for r in conn.execute("SELECT * FROM assets ORDER BY kind DESC, name")]
    snaps: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM asset_snapshots ORDER BY date, id"):
        snaps.setdefault(r["asset_id"], []).append(dict(r))

    tot = {"reserve": 0.0, "investment": 0.0}
    interest = tanb_w = tanb_base = gain = invested_sum = 0.0
    for a in assets:
        s = snaps.get(a["id"], [])
        last = s[-1] if s else None
        a["value"] = last["value"] if last else None
        a["as_of"] = last["date"] if last else None
        a["invested"] = last["invested"] if last else None
        a["gain"] = a["gain_pct"] = a["est_interest_year"] = None
        if a["value"] is not None:
            tot[a["kind"]] += a["value"]
            if a["tanb"] is not None:
                a["est_interest_year"] = a["value"] * a["tanb"] / 100
                interest += a["est_interest_year"]
                tanb_w += a["value"] * a["tanb"]
                tanb_base += a["value"]
            if a["kind"] == "investment" and a["invested"]:
                a["gain"] = a["value"] - a["invested"]
                a["gain_pct"] = a["gain"] / a["invested"]
                gain += a["gain"]
                invested_sum += a["invested"]
        a["history"] = [{"id": h["id"], "date": h["date"], "value": h["value"], "invested": h["invested"]} for h in s]

    dates = sorted({h["date"] for lst in snaps.values() for h in lst})
    series = []
    for d in dates:
        row = {"reserve": 0.0, "investment": 0.0}
        for a in assets:
            cur = None
            for h in snaps.get(a["id"], []):
                if h["date"] <= d:
                    cur = h
            if cur:
                row[a["kind"]] += cur["value"]
        series.append({"date": d, **row, "total": row["reserve"] + row["investment"]})

    return {
        "assets": assets,
        "series": series,
        "totals": {
            "reserve": tot["reserve"],
            "investment": tot["investment"],
            "total": tot["reserve"] + tot["investment"],
            "est_interest_year": interest,
            "weighted_tanb": tanb_w / tanb_base if tanb_base else None,
            "gain": gain if invested_sum else None,
            "gain_pct": gain / invested_sum if invested_sum else None,
        },
    }
