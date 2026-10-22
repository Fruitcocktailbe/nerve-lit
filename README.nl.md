# nerve-lit (Nederlands)

> English version: [README.md](README.md).

Persoonlijke, semantisch doorzoekbare literatuurdatabank van **peripheral-nerve-surgery** — bevraagbaar via JARVIS, van overal.

- **Wat:** PubMed harvest → OA full-text (PMC/Unpaywall) → embeddings (Ollama, lokaal) → LanceDB → MCP-tool in JARVIS.
- **Waarom los van SONA:** persoonlijke researchbib, lokaal, houdt SONA's prod-DB proper.
- **Regels:** zie [CLAUDE.md](CLAUDE.md) — o.a. géén Sci-Hub; paywalled full-text via UZB-toegang.
- **Bouwvolgorde + status:** zie [PLAN.md](PLAN.md).

## Setup

```powershell
uv sync --extra index --extra mcp     # basis + PDF/LanceDB + MCP-server
uv sync --extra rerank                # optioneel: reranker (~2.5 GB torch)
copy .env.example .env                # en invullen
ollama pull bge-m3                    # embedder, 1024-dim
```

`.env` heeft minstens `NCBI_EMAIL`, `UNPAYWALL_EMAIL` en `PDF_DIR` nodig. Een
[NCBI API-key](https://www.ncbi.nlm.nih.gov/account/settings/) is gratis en tilt
de rate-limit van 3 naar 10 requests per seconde.

## Gebruik

```powershell
# Fase 1 — metadata (idempotent, resumebaar)
uv run python -m harvester.harvest count --query scope    # hoeveel treffers?
uv run python -m harvester.harvest run   --query scope    # esearch + efetch
uv run python -m harvester.harvest stats

# Fase 2 — full-text via legale OA-routes
uv run python -m harvester.fulltext fetch
uv run python -m harvester.fulltext export                # needs-institutional.csv

# Fase 3 — index
uv run python -m index.build run
uv run python -m index.build run --retry-failed     # na een onderbreking
uv run python -m index.build stats

# Zoeken zonder JARVIS
uv run python -m index.search "supercharged end-to-side AIN naar ulnaris" -k 8

# Kwaliteitscontrole
uv run python -m harvester.report coverage
uv run python -m harvester.report sample --order random

# Per-artikel statuslijst (Excel/CSV): waar staat elk artikel in de pijplijn?
uv run python -m harvester.report export                        # -> data/status.csv
uv run python -m harvester.report export --out "H:\My Drive\nerve-lit-status.csv"

uv run pytest -q
```

`status.csv` heeft één rij per artikel met o.a. `fetch_status` (pending / oa_pmc /
oa_unpaywall / needs-institutional), `index_status` en `searchable_on` (abstract of
fulltext). Filter/sorteer in Excel om te zien wat gedownload, geïndexeerd of nog
open is. Het is een momentopname — draai het opnieuw voor de actuele stand.

Draai **niet** twee indexeer-processen tegelijk: LanceDB verdraagt maar één
schrijver per tabel en klaagt dan over het version-hint-bestand.

Elke stap mag je onderbreken en opnieuw starten: de status staat per artikel in
SQLite (`data/metadata/nerve_lit.db`), en afgewerkte zoekvensters worden gelogd.

## Hoe het in elkaar zit

```
harvester/   queries.py  de scope-query (~41k artikels)
             ncbi.py     rate-limited E-utilities-client
             parse.py    PubMed-XML -> dicts
             db.py       SQLite: metadata + pipeline-status
             harvest.py  fase 1 CLI
             fulltext.py fase 2 CLI (OA-cascade)
             report.py   inspectie van de store
index/       extract.py  JATS/PDF -> chunks
             embed.py    Ollama bge-m3 (1024-dim)
             store.py    LanceDB
             build.py    fase 3 CLI
             search.py   kNN + optionele reranker
mcp-server/  server.py   fase 4: MCP-tools voor JARVIS
data/        metadata/nerve_lit.db, lancedb/   (gitignored)
```

### De full-text-cascade

Per artikel, stoppen bij het eerste succes:

1. **PMC full-text (JATS-XML)** via `efetch db=pmc`. NCBI serveert de `<body>`
   alleen voor artikels waarvoor het de rechten heeft — die grens wordt dus
   server-side afgedwongen. XML gaat vóór PDF omdat het echte sectietitels
   heeft en geen tweekolomsrommel.
2. **Unpaywall** op DOI → beste OA-PDF-locatie.
3. Geen OA → status `needs-institutional`, **geen** download. Die lijst exporteer
   je met `fulltext export` naar CSV voor batch-ophaling via UZB.

Artikels zonder full-text worden alsnog geïndexeerd op hun **abstract** — met
~85% abstract-dekking blijft zo het hele corpus vindbaar, ook vóór de UZB-batch
binnen is. Elke hit vermeldt of hij uit `jats`, `pdf` of `abstract` komt.

## Aansluiten op JARVIS

De MCP-server praat stdio en draait op deze machine; Tailscale dekt de toegang
van buitenaf al af.

Voeg deze entry toe aan **`D:\GithubLocal\J.A.R.V.I.S\.mcp.json`** (naast de
bestaande `gworkspace-personal`):

```json
"nerve-lit": {
  "command": "D:\\GithubLocal\\nerve-lit\\.venv\\Scripts\\python.exe",
  "args": ["D:\\GithubLocal\\nerve-lit\\mcp-server\\server.py"]
}
```

Daarna JARVIS herstarten (tray-icoon → *Restart JARVIS*): MCP-servers starten één
keer per sessie, dus een draaiende backend pikt de nieuwe entry niet op.

`enableAllProjectMcpServers` staat al op `true` in `.claude/settings.local.json`,
dus er hoeft niets aan een allowlist toegevoegd te worden.

Tools: `search_nerve_lit(query, k, year_from, year_to, full_text_only)`,
`get_nerve_lit_article(pmid)`, `get_nerve_lit_stats()`.

> **Hernoem deze tools niet.** JARVIS' Trust Ledger leidt uit de tóólnaam af of
> een MCP-call alleen leest (`voice-agent/backend/src/agent.ts`, `READ_VERB`).
> Beide launchers zetten `JARVIS_EXPOSED=1`, en in die modus vraagt elke tool die
> níét als read herkend wordt om handmatige bevestiging. Een naam als
> `nerve_lit_search` zou dus betekenen dat je elke literatuurvraag met de hand
> moet goedkeuren. `tests/test_mcp_server.py` bewaakt dit.

### Snelheid

Een zoekopdracht kost **~1,8 s** warm: 0,2 s query-embedding + 0,1 s kNN over
LanceDB + 1,5 s reranken van 50 kandidaten op de GPU. De eerste zoekopdracht na
het opstarten kost ~30 s omdat het rerankermodel en de LanceDB-pagina's dan van
schijf komen — de MCP-server doet dat daarom in een achtergrondthread bij het
starten, zodat Jeroens eerste vraag die wachttijd niet ziet.

De reranker heeft **CUDA** nodig om bruikbaar te zijn (op CPU duurt hij ~45 s per
vraag). PyPI serveert op Windows een CPU-only torch, dus `pyproject.toml` wijst
torch expliciet naar de CUDA-wheels. Controleren:

```powershell
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# verwacht: 2.11.0+cu128 True
```

Zonder CUDA slaat de zoeklaag reranking automatisch over in plaats van het
antwoord 45 s op te houden; `RERANK_ON_CPU=1` forceert het toch.
