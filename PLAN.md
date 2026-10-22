# nerve-lit — Bouwplan

> Status: **fases 1–4 compleet en volledig gedraaid op 2026-08-01.** Alle 41.139 artikels hebben metadata; de full-text-fetch is over het hele corpus gelopen (4.159 full-text bestanden op H:, 36.980 op de `needs-institutional`-werklijst); 34.777 artikels zijn geïndexeerd (88.205 chunks), waarvan 4.145 op hun volledige tekst mét figuur-/tabelbijschriften. Zoeken werkt end-to-end via de MCP-server (in JARVIS geregistreerd) in ~1,8 s met GPU-reranking, en levert nu ook Methods-/techniekpassages op, niet enkel abstracts.
>
> De 6.362 artikels zonder `searchable_on` hebben géén abstract én geen OA-full-text — die worden pas doorzoekbaar als er via UZB full-text voor binnenkomt.
>
> Wat rest: de UZB-full-text-batch (werklijst `data/needs-institutional.csv`, 36.980 items) en de eindtest vanaf de telefoon via Tailscale.

## Doel

Een lokale, semantisch doorzoekbare full-text databank van peripheral-nerve-surgery-literatuur, bevraagbaar via JARVIS van overal.

## Fase 0 — Setup (eenmalig, Jeroen aan het toetsenbord)

- [x] **Ollama** draait, `bge-m3` staat lokaal (geverifieerd 2026-07-23).
- [x] **Reranker** `bge-reranker-v2-m3` geïnstalleerd en draaiend op de RTX 3070 (torch 2.11.0+cu128). Let op: PyPI geeft op Windows een CPU-only torch — zie CLAUDE.md regel 8.
- [ ] **NCBI API-key** aanmaken (gratis, verhoogt rate-limit 3→10 req/s) → in `.env`. Nu nog leeg, dus alles loopt op 3 req/s.
- [x] **Unpaywall**: `email=`-param staat in `.env`.
- [x] **Google Drive for Desktop**: `PDF_DIR` wijst naar de map `nerve-lit-pdfs` in de Drive van de eigenaar (de juiste driveletter checken, zie AGENTS.md regel 9; het corpus stond even op de verkeerde account en is op 2026-07-23 verhuisd).
- [x] **Schijfruimte**: ~330 GB vrij op die Drive, ruim genoeg voor de geschatte 8–24 GB.

## Fase 1 — Harvest + metadata (`harvester/`)

**Gekozen scope-query — O1 breed, Engels + humaan, GEEN tijdslimiet (live getest 2026-07-22, keuze Jeroen):**
Het hele veld, niets thematisch uitgesloten, alle jaren. Enkel Engels + humane studies (geen dierstudies).
```
("Peripheral Nerves/surgery"[Mesh] OR "Peripheral Nerve Injuries/surgery"[Mesh]
 OR "Nerve Compression Syndromes/surgery"[Mesh] OR "nerve transfer"[tiab]
 OR "nerve graft*"[tiab] OR neurotization[tiab] OR neurorrhaphy[tiab]
 OR "nerve repair"[tiab] OR "nerve reconstruction"[tiab] OR "brachial plexus"[tiab]
 OR "regenerative peripheral nerve interface"[tiab] OR RPNI[tiab]
 OR "targeted muscle reinnervation"[tiab]
 OR ("facial nerve"[tiab] AND (reanimation OR reconstruction OR palsy OR grafting))
 OR "nerve decompression"[tiab] OR "nerve entrapment"[tiab])
AND English[lang] AND humans[mesh]
```

**Volume (live counts 2026-07-22):**
- Gekozen scope: **~41.139** artikels → metadata voor allemaal.
- Gratis full-text (`AND free full text[filter]`): **~7.904** → auto-downloadbaar (~8–24 GB).
- Rest (~33.000): abstract-only + `needs-institutional`-werklijst.
- Klein starten voor de dry-run kan op `AND review[pt]` (~5k reviews) of op de reconstructieve kern.

- [x] `esearch` → `efetch` → metadata per artikel. Live counts 2026-07-23 identiek aan die van 22-07: 41.139 / 7.904.
- [x] SQLite-store (`data/metadata/nerve_lit.db`), idempotent en resumebaar. De werklijst kwam exact op 41.139 uit — de datumvensters dekken de zoekset dus zonder gaten of dubbels.
- [x] Dry-run + volledige harvest: **41.139/41.139 records binnen**. Dekking: titel/journal/jaar/MeSH 100%, DOI 86,9%, abstract 84,5%, PMC-ID 12,2%.

## Fase 2 — Full-text fetch (`harvester/`)

Cascade per artikel, in volgorde, stop bij eerste succes:
1. **PMC full-text** — `efetch db=pmc` → JATS-XML. *Afgeweken van het oorspronkelijke plan:* de OA-package-service geeft `ftp://`-links die over https 404'en, en XML is sowieso beter dan PDF om te chunken (echte sectietitels). NCBI serveert de `<body>` enkel als het de rechten heeft.
2. **Unpaywall** — query op DOI → beste OA-PDF-link → download.
3. **Geen OA** → status `needs-institutional`; **geen** automatische download.

- [x] Cascade werkt; bestanden → `G:\My Drive\nerve-lit-pdfs\<jaar>\<pmid>_<auteur>.{xml,pdf}`, pad + formaat + status in de store.
- [x] Downloads gevalideerd op magic bytes — uitgevers serveren cookiewalls met HTTP 200.
- [x] Export `needs-institutional.csv` (PMID, DOI, titel, tijdschrift, jaar, PubMed- en DOI-URL).
- [ ] **Nog te doen:** de volledige fetch over alle 41k draaien. Getest op een steekproef van 40 (16 PMC + 3 Unpaywall). Duurt uren op 3 req/s — eerst de NCBI-key zetten.
- [ ] UZB-linkresolver-URL in de CSV zetten zodra Jeroen die doorgeeft; nu staat de DOI-URL erin.
- [x] **Nooit Sci-Hub** (CLAUDE.md regel 1).

## Fase 3 — Index (`index/`)

- [x] JATS-XML → sectie-aware chunks; PDF → PyMuPDF met paginanummers. GROBID bleek niet nodig: PMC levert de structuur al kant-en-klaar.
- [x] Chunking ±650 tokens met 20% overlap, afbreken op zinsgrens. Referentielijsten worden eruit gefilterd (zie CLAUDE.md).
- [x] **Abstract-fallback**: artikels zonder full-text worden op hun abstract geïndexeerd, zodat het hele corpus vindbaar is en niet enkel de ~8k OA-artikels. Elke hit zegt of hij uit `jats`, `pdf` of `abstract` komt.
- [x] Embeddings via Ollama `bge-m3` (1024-dim), gebatcht over artikels heen (~18 artikels/s).
- [x] Wegschrijven naar **LanceDB** (`data/lancedb/`) met titel, jaar, journal, DOI, sectiebron en pagina.
- [x] Idempotent: status per artikel in SQLite, en herindexeren gooit eerst de oude chunks van die PMID weg.

## Fase 4 — Search via JARVIS (`mcp-server/`)

- [x] MCP-server met `search_nerve_lit(query, k, year_from, year_to, full_text_only)`, `get_nerve_lit_article(pmid)` en `get_nerve_lit_stats()`. **In Python, niet Node** — de reranker (`sentence-transformers`) bestaat alleen in Python, en zo delen server en indexer dezelfde zoekcode in plaats van ze twee keer te schrijven. Namen beginnen bewust met een leeswerkwoord: JARVIS' Trust Ledger zou ze anders als schrijfactie zien en om bevestiging vragen bij élke vraag.
- [x] Server draait, getest met een echte MCP-client over stdio: tools worden gelijst, `nerve_lit_stats` en `nerve_lit_article` geven correcte data, `nerve_lit_search` geeft passages mét citaat.
- [x] Lokale done-check gehaald: *"supercharged end-to-side AIN to ulnar nerve transfer for cubital tunnel syndrome"* geeft als top-3 de PRS-outcomestudie (2020), de Canadese survey (2024) en de gerandomiseerde J Neurosurg-vergelijking (2022) — scores 0,99+ na reranking.
- [ ] Registreren in de JARVIS voice-agent MCP-config (`D:\GithubLocal\J.A.R.V.I.S\voice-agent\...`) — snippet staat in [README.md](README.md).
- [ ] Eindtest: dezelfde vraag vanaf de telefoon via Tailscale aan JARVIS stellen.

## Latere opties (niet nu)

- Volledig offline Q&A: Ollama-chatmodel naast de retrieval.
- Herindexeren als de UZB-full-text-batch binnen is.
- Koppeling met de bestaande Drive-catalog (`J.A.R.V.I.S/references/clinical-index.md`) zodat je eigen PLH-nerve-map mee-geïndexeerd wordt.
