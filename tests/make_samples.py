"""Gera ficheiros de exemplo (Excel, CSV, PDF com texto, PDF digitalizado) em data/samples."""
import random
from datetime import date
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "data" / "samples"
OUT.mkdir(parents=True, exist_ok=True)
random.seed(7)


def month_rows(y, m):
    r = [
        (date(y, m, 1), "RENDA APARTAMENTO", -550.0),
        (date(y, m, 3), "PAGAMENTO SERVICOS FATURA RECIBO", round(random.uniform(1500, 2500), 2)),
        (date(y, m, 5), "EDP COMERCIAL", -round(random.uniform(40, 70), 2)),
        (date(y, m, 5), "VODAFONE", -29.99),
        (date(y, m, 6), "NETFLIX.COM", -13.99),
        (date(y, m, 6), "SPOTIFY", -6.99),
        (date(y, m, 8), "GINASIO HOLMES PLACE", -39.9),
        (date(y, m, 10), "TRADING212 TRANSFERENCIA", -200.0),
        (date(y, m, 12), "SEGURANCA SOCIAL", -round(random.uniform(150, 260), 2)),
    ]
    for d in (4, 9, 14, 19, 24, 28):
        r.append((date(y, m, d), random.choice(["CONTINENTE MODELO", "PINGO DOCE", "LIDL"]), -round(random.uniform(18, 75), 2)))
    for d in (7, 13, 21, 26):
        r.append((date(y, m, d), random.choice(["UBER *TRIP", "GLOVO", "RESTAURANTE O ZE", "BOLT"]), -round(random.uniform(6, 38), 2)))
    r.append((date(y, m, 22), "AMAZON PT", -round(random.uniform(15, 90), 2)))
    return sorted(r, key=lambda x: x[0])


# 1) Excel — débito/crédito/saldo, abril–agosto 2026
rows, bal = [], 1800.0
for m in range(4, 9):
    for d, desc, amt in month_rows(2026, m):
        bal = round(bal + amt, 2)
        rows.append({"Data Mov.": d, "Descrição": desc, "Débito": -amt if amt < 0 else None, "Crédito": amt if amt > 0 else None, "Saldo": bal})
pd.DataFrame(rows).to_excel(OUT / "extrato_abr_ago.xlsx", index=False)

# 2) CSV — ';' e vírgula decimal, outro cartão, junho
csv = ["Data;Descritivo;Montante"]
for d, desc, amt in month_rows(2026, 6)[:0] + [(date(2026, 6, 15), "ZARA", -49.9), (date(2026, 6, 16), "FNAC CHIADO", -23.5), (date(2026, 6, 20), "CINEMA NOS", -14.0)]:
    csv.append(f"{d:%d-%m-%Y};{desc};{str(amt).replace('.', ',')}")
(OUT / "cartao_junho.csv").write_text("\n".join(csv), encoding="utf-8")

# 3) PDF com camada de texto — setembro, sem sinais, só saldo
lines = [(d, desc, amt) for d, desc, amt in month_rows(2026, 9)]
c = canvas.Canvas(str(OUT / "extrato_setembro_texto.pdf"), pagesize=A4)
c.setFont("Helvetica", 9)
y = 800
c.drawString(40, y, "EXTRATO DE CONTA - SETEMBRO 2026"); y -= 24
c.drawString(40, y, "DATA MOV   DATA VALOR   DESCRICAO                                  VALOR        SALDO"); y -= 16
bal = 1500.0
for d, desc, amt in lines:
    bal = round(bal + amt, 2)
    fmt = lambda v: f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    c.drawString(40, y, f"{d:%d-%m-%Y}  {d:%d-%m-%Y}  {desc:<40} {fmt(amt):>10} {fmt(bal):>12}")
    y -= 14
c.save()

# 4) PDF digitalizado (imagem) — outubro, com sinais, para testar OCR
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 28)
img = Image.new("RGB", (1700, 700), "white")
dr = ImageDraw.Draw(img)
oc = [
    ("01-10-2026", "RENDA APARTAMENTO", "-550,00"),
    ("02-10-2026", "CONTINENTE MODELO", "-62,35"),
    ("03-10-2026", "SALARIO EMPRESA XPTO", "2.150,00"),
    ("05-10-2026", "NETFLIX.COM", "-13,99"),
    ("08-10-2026", "FARMACIA CENTRAL", "-18,40"),
    ("12-10-2026", "UBER EATS", "-24,10"),
]
dr.text((40, 30), "EXTRATO OUTUBRO 2026 (digitalizado)", fill="black", font=font)
for i, (d, desc, v) in enumerate(oc):
    dr.text((40, 100 + i * 50), f"{d}  {desc:<28} {v:>10}", fill="black", font=font)
img.save(OUT / "extrato_outubro_scan.pdf", resolution=200)
print("ok ->", sorted(p.name for p in OUT.iterdir()))
