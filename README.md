# nerve-lit

A personal, semantically searchable literature database for **peripheral nerve surgery**: about 41,000 PubMed records, open-access full text where it exists, embedded locally and served to an AI assistant as an MCP server.

Built by a plastic surgeon for his own research. Everything runs on one machine; no article text ever leaves it.

> Nederlandse versie: [README.nl.md](README.nl.md).

## Pipeline

```
PubMed (E-utilities)  →  SQLite metadata  →  full text (PMC OA / Unpaywall)  →  chunks
   →  embeddings (bge-m3 via Ollama, 1024-dim)  →  LanceDB  →  kNN + reranker  →  MCP tools
```

1. **Harvest.** A scope query on PubMed (`harvester/queries.py`) is fetched through the rate-limited E-utilities client and parsed into SQLite (metadata plus a per-article pipeline status). Idempotent and resumable.
2. **Full text, legal routes only.** Per article, stop at the first hit: PMC open-access JATS XML (NCBI only serves the body where it holds the rights), then the best open-access PDF via Unpaywall. Anything else is marked `needs-institutional` and exported as a CSV for manual retrieval through a library subscription. No scraping, no Sci-Hub.
3. **Index.** JATS or PDF is split into ~650-token chunks with 20 % overlap on sentence boundaries; reference lists, licence blocks and cover pages are stripped. Articles without full text are indexed on their abstract, so the whole corpus stays findable. Embeddings come from `bge-m3` running locally in Ollama; vectors live in LanceDB next to the SQLite metadata.
4. **Search.** kNN over LanceDB (top 50), then `BAAI/bge-reranker-v2-m3` on the GPU re-ranks to the best 8. About 1.8 s warm. Without CUDA the reranker is skipped instead of taking 45 s on CPU.
5. **Serve.** A FastMCP server exposes three read-only tools: `search_nerve_lit(query, k, year_from, year_to, full_text_only)`, `get_nerve_lit_article(pmid)`, `get_nerve_lit_stats()`. Every hit says whether it came from `jats`, `pdf` or `abstract`.

## Layout

```
harvester/   queries.py  scope query (~41k articles)
             ncbi.py     rate-limited E-utilities client
             parse.py    PubMed XML -> dicts
             db.py       SQLite: metadata + pipeline status
             harvest.py  phase 1 CLI
             fulltext.py phase 2 CLI (open-access cascade)
             report.py   coverage, samples, status export
index/       extract.py  JATS/PDF -> chunks
             embed.py    Ollama bge-m3
             store.py    LanceDB
             build.py    phase 3 CLI
             search.py   kNN + optional reranker
mcp-server/  server.py   phase 4: MCP tools
tests/                   parsing, chunking, tool-name contract
data/                    SQLite + LanceDB (gitignored, rebuildable)
```

The corpus (PDFs, XML, index) is never committed: it is rebuildable from the pipeline and lives in a synced folder outside the repo.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com) and, for the reranker, an NVIDIA GPU with CUDA.

```powershell
uv sync --extra index --extra mcp     # base + PDF/LanceDB + MCP server
uv sync --extra rerank                # optional: reranker (~2.5 GB torch, CUDA wheels)
copy .env.example .env                # then fill it in
ollama pull bge-m3
```

`.env` needs at least `NCBI_EMAIL`, `UNPAYWALL_EMAIL` and `PDF_DIR`. An [NCBI API key](https://www.ncbi.nlm.nih.gov/account/settings/) is free and raises the rate limit from 3 to 10 requests per second. Both NCBI and Unpaywall ask callers to identify themselves with an e-mail address.

## Usage

```powershell
# Phase 1: metadata
uv run python -m harvester.harvest count --query scope
uv run python -m harvester.harvest run   --query scope
uv run python -m harvester.harvest stats

# Phase 2: full text through open-access routes
uv run python -m harvester.fulltext fetch
uv run python -m harvester.fulltext export        # -> needs-institutional.csv

# Phase 3: index
uv run python -m index.build run
uv run python -m index.build run --retry-failed
uv run python -m index.build stats

# Search from the command line
uv run python -m index.search "supercharged end-to-side AIN to ulnar transfer" -k 8

# Quality control and a per-article status export
uv run python -m harvester.report coverage
uv run python -m harvester.report sample --order random
uv run python -m harvester.report export          # -> data/status.csv

uv run pytest -q
```

Every step can be interrupted and restarted: status is kept per article in SQLite, and finished search windows are logged. Do not run two indexing processes at once; LanceDB allows one writer per table.

## Connect it to an MCP client

The server speaks stdio. Add it to your client's MCP configuration, for example:

```json
"nerve-lit": {
  "command": "<repo>/.venv/Scripts/python.exe",
  "args": ["<repo>/mcp-server/server.py"]
}
```

Keep the tool names as they are: they start with a read verb (`search_`, `get_`) so that a client's permission layer can treat them as read-only. `tests/test_mcp_server.py` guards that contract.

## Performance notes

- Warm search: ~0.2 s query embedding + ~0.1 s kNN + ~1.5 s reranking of 50 candidates on an RTX 3070.
- First search after start-up: ~30 s (reranker weights and LanceDB pages come off disk). The MCP server warms this in a background thread at start.
- On Windows, PyPI serves a CPU-only torch; `pyproject.toml` pins torch to the CUDA index. Check with `uv run python -c "import torch; print(torch.cuda.is_available())"`.

## Licence and data

Code: [MIT](LICENSE). Article text is not part of this repository. Open-access articles carry their own licences (PMC OA subset, publisher OA terms); the pipeline fetches them only through NCBI and Unpaywall and keeps them local. Paywalled articles are never downloaded automatically.
