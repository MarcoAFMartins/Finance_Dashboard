# Finance Dashboard

Dashboard de finanças pessoais, local e privado. Backend Python (FastAPI + SQLite), frontend HTML único (Chart.js).

## Arrancar com Docker (recomendado)

```bash
docker compose up -d --build     # abre http://localhost:8010 ; reinicia sozinho com a máquina
docker compose logs -f           # ver logs
docker compose down              # parar
```

- A BD fica em `./data/finance.db` (volume), por isso sobrevive a rebuilds.
- Só escuta em `127.0.0.1`: não expor à rede, não há autenticação.
- LLM: o compose liga-se ao Ollama no host (`host.docker.internal:11434`). Corre `ollama pull qwen2.5:7b-instruct`
  no host. Para desligar o LLM: `FIN_LLM= docker compose up -d`.

## Arrancar sem Docker

```bash
# dependências de sistema (OCR)
sudo apt install tesseract-ocr tesseract-ocr-por poppler-utils    # macOS: brew install tesseract tesseract-lang poppler

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python tests/make_samples.py     # opcional: ficheiros de exemplo em data/samples
uvicorn server:app --reload      # abre http://localhost:8010
```

A base de dados fica em `data/finance.db` (muda com `FIN_DB=/caminho/x.db`).

## Fluxo

1. **Importar**: larga Excel/CSV, PDF (com texto ou digitalizado) ou imagem.
   - Excel/CSV: deteta o cabeçalho (Data, Descrição, Montante ou Débito/Crédito), formatos PT (`1.234,56`, `dd-mm-aaaa`).
   - PDF: usa a camada de texto; se a página não tiver texto, faz OCR (Tesseract, 300 dpi).
   - Extratos sem sinal: o sinal vem da variação do saldo; sem saldo, de palavras-chave.
   - Re-importar o mesmo ficheiro ou movimentos já existentes não duplica.
2. **Configuração**: define o saldo inicial de cada conta (necessário para runway e património).
3. **Movimentos**: corrige categorias; ao corrigir podes criar uma regra para o futuro.

## KPIs

Rendimento, despesas, poupança e taxa de poupança (mês e média 3M), burn rate 3M, runway (saldo líquido ÷ burn),
património líquido, despesas fixas (% do rendimento), 50/30/20, volatilidade do rendimento (CV 6M),
reserva fiscal (% configurável), custos recorrentes detetados (valor anual), maiores despesas.
Transferências internas ficam fora de rendimento/despesa; categoria `Investimento` conta como poupança.

## LLM local (opcional, desligado por defeito)

Com [Ollama](https://ollama.com):

```bash
ollama pull qwen2.5:7b-instruct
FIN_LLM=qwen2.5:7b-instruct uvicorn server:app --reload
```

Só entra como fallback, depois das regras: (1) categorizar descrições que nenhuma regra apanha (marcadas com badge *LLM*
para reveres), (2) extrair movimentos de PDFs/OCR que o parser não consegue ler. Tudo corre local.

## Vários bancos

Cada banco é uma conta: no **Importar** escreve o banco (ex.: `Millennium`) antes de largar os ficheiros. O mesmo nome
junta tudo na mesma conta; um nome novo cria outra. Em **Movimentos** filtras por banco, e a **Visão geral** mostra o saldo
de cada conta. Define o saldo inicial de cada uma em **Configuração**.

## Reserva e investimentos (separador à parte)

Fica fora dos KPIs e do saldo da visão geral. Para cada ativo (conta reserva, ETF, etc.) guardas o valor atual numa data;
o histórico dá a evolução. Reservas têm TANB (bruta, % ao ano): os juros estimados são `valor × TANB` (simplificado,
sem capitalização nem retenção). Investimentos podem ter o capital aplicado, para mostrar o ganho.

## Impostos (campo no topo)

Percentagem do rendimento que pões de lado para impostos/Segurança Social; o KPI mostra `rendimento × %`. É uma estimativa,
não um cálculo fiscal: o valor certo depende do teu regime e escalão. Põe 0 para esconder o KPI.

## Perguntar (chat sobre os dados)

Separador **Perguntar**: o LLM escreve uma query SELECT sobre a vista `v_tx`, o backend valida-a (só `SELECT`/`WITH`,
uma instrução, sem palavras de escrita) e executa-a numa ligação SQLite **só de leitura**. Mostra sempre a query usada e a
tabela de resultados, para confirmares. Se a query falhar, o erro volta ao modelo para uma segunda tentativa.

## Testes

```bash
python tests/test_pipeline.py    # ingestão + OCR + KPIs
python tests/test_ask.py         # validação/só-leitura do Perguntar (LLM simulado)
python tests/test_assets.py      # reserva e investimentos
```

## Estrutura

```
server.py          API + serve web/index.html
web/index.html     frontend (HTML/CSS/JS, Chart.js via CDN)
fin/db.py          esquema SQLite, categorias e regras iniciais
fin/ingest.py      Excel/CSV/PDF/imagem -> transações
fin/ocr.py         texto do PDF, OCR com Tesseract
fin/categorize.py  regras + fallback LLM
fin/kpis.py        cálculo dos KPIs
fin/llm.py         cliente Ollama
```

Dados reais: mete-os fora do git (`data/real/` está no `.gitignore`).
