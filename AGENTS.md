# nerve-lit — Zenuwchirurgie literatuurdatabank

Persoonlijke, doorzoekbare literatuurdatabank voor Jeroen Poelaert over **peripheral nerve surgery** (zijn subspecialty: nerve transfers, RPNI/AMI, facialis, brachial plexus). Doel: alle relevante artikels harvesten, full-text opslaan, en semantisch bevragen — van overal, via JARVIS.

Dit is de **werkplaats**. De strategie/beslissing leeft in de cockpit (`D:\GithubLocal\J.A.R.V.I.S`, `decisions/log.md` 2026-07-22). Zie [PLAN.md](PLAN.md) voor de bouwvolgorde.

## Architectuur (kort)

- **Harvest** — PubMed E-utilities (esearch/efetch) → metadata (PMID, DOI, PMC, MeSH, abstract).
- **Fetch** — PDF-cascade: PMC Open Access → Unpaywall → rest markeren `needs-institutional` (via UZB-toegang, niet automatisch). PDF's → lokale map die **Google Drive for Desktop** synct.
- **Index** — PDF→tekst (PyMuPDF/GROBID) → chunks → embeddings `bge-m3` (1024-dim) via **Ollama** (lokaal, RTX 3070) → **LanceDB** (embedded vector store). Query-tijd: top-50 ophalen → herrangschikken naar top-8 met `bge-reranker-v2-m3`.
- **Search** — een **MCP-server** (`nerve-lit-search`) geregistreerd in de JARVIS voice-agent-backend. Bevraging + antwoord-synthese door **Claude/JARVIS**, van overal via de bestaande **Tailscale**. Geen aparte netwerk-service.

## Bron- & data-regels

1. **De harvester automatiseert alleen legale bronnen.** Full-text-download via **PMC Open Access → Unpaywall**; paywalled items worden gemarkeerd als `needs-institutional` en verzameld in een werklijst (DOI + UZB-resolverlink) voor Jeroens institutionele UZB-toegang. De pipeline downloadt **niet** automatisch van piraterij-mirrors (Sci-Hub e.d.) — dat is de grens van wat hier gebouwd wordt. Wat Jeroen daarna manueel met de werklijst doet, valt buiten de scope van deze tooling. (decisions/log.md 2026-07-22)
2. **Respecteer API-rate-limits.** NCBI E-utilities: max 3 req/s zonder key, 10 met key. Unpaywall: verplicht een `email`-parameter meesturen. Nette `User-Agent`. Niet hameren.
3. **Data nooit in git.** `data/` (PDF's + LanceDB-index) en secrets zijn gitignored. Het corpus leeft in de Drive-gesyncte map; de index is herbouwbaar.
4. **Geen patiëntdata.** Dit corpus is uitsluitend publiek gepubliceerde literatuur.

## Harde technische regels (uit de bouw geleerd — niet opnieuw ontdekken)

1. **Embedder-model ligt vast op `bge-m3` (1024-dim).** Wisselen van embedder betekent
   het **hele** corpus herembedden. Een index met vectoren van twee modellen geeft geen
   foutmelding, maar stil onzin. Zelfde geldt voor de chunkgrootte.
2. **PubMed paginatie stopt op 10.000.** `retstart + retmax > 10000` weigert PubMed binnen
   één zoekopdracht. Grotere sets moeten in datumvensters (`datetype=edat` + `mindate`/
   `maxdate`) opgesplitst worden — dat doet `harvest.collect_pmids` recursief. Een venster
   mag pas als 'done' gelogd worden als **alle** pagina's binnen zijn, anders mist het
   corpus stilzwijgend artikels bij een volgende run.
3. **De PMC OA-package-mirror is dood voor ons.** `oa.fcgi` geeft `ftp://`-links naar
   `oa_package/*.tar.gz`; die host 404't over https. Gebruik `efetch db=pmc` voor de
   JATS-XML. Bijkomend voordeel: NCBI serveert de `<body>` alleen als het de rechten
   heeft, dus de OA-grens uit regel 1 wordt server-side afgedwongen.
4. **Referentielijsten mogen nooit de index in.** Ze staan vol vaktermen en scoren dus
   hoog op élke zoekopdracht, terwijl ze niets beantwoorden. `index/extract.py` kapt ze
   af op de kop én filtert restanten op markerdichtheid. Bij twijfel: eerst controleren
   met `python -m index.search`, niet blind opnieuw indexeren.
5. **Download-resultaten valideren op magic bytes.** Uitgevers serveren cookiewalls en
   landingspagina's met HTTP 200. Zonder `%PDF`-check vult de Drive-map zich met HTML.
6. **Eén batch-fout mag nooit een hele groep kosten.** Ollama geeft sporadisch een 400 op
   een batch waarvan élke tekst afzonderlijk wél embedt. `index/embed.py` splitst een
   falende batch daarom in tweeën tot de kapotte tekst geïsoleerd is, en `index/build.py`
   valt terug op artikel-per-artikel. Dit geldt breder: in een run van uren is "sla het
   ene kapotte item over" altijd beter dan "laat de groep vallen".
7. **Maar één schrijver tegelijk op LanceDB.** Twee indexeer-processen op dezelfde tabel
   geven `Access is denied` op het version-hint-bestand. De schrijf zelf overleeft het,
   maar draai het niet bewust parallel.
8. **Reranker draait op de GPU, of niet.** Op CPU kost reranken ~45 s per vraag tegenover
   ~1,5 s op de RTX 3070 — onbruikbaar als JARVIS erop wacht. PyPI serveert op Windows
   een CPU-only torch, dus `pyproject.toml` pint torch aan de CUDA-index én vermeldt hem
   expliciet als dependency (via `sentence-transformers` alleen negeert uv die index).
   Na elke `uv sync` die torch aanraakt: `torch.cuda.is_available()` controleren.
9. **Het corpus hoort in de Drive-map van de eigenaar**, niet in de repo. Google Drive for
   Desktop geeft elke gemounte account een eigen driveletter, en die letters kunnen
   verspringen — verifieer met `Get-PSDrive -PSProvider FileSystem | Select Name, Description`
   (de Description toont het account) vóór je `PDF_DIR` zet.
10. **MCP-toolnamen beginnen met een leeswerkwoord** (`search_`, `get_`). JARVIS' Trust
    Ledger (`voice-agent/backend/src/agent.ts`, `READ_VERB`) leidt uit de naam ná
    `mcp__<server>__` af of een call alleen leest. Beide launchers zetten
    `JARVIS_EXPOSED=1`, en dan moet elke niet-herkende tool handmatig bevestigd worden —
    wat hands-free bevragen onmogelijk maakt. `tests/test_mcp_server.py` bewaakt dit.
11. **Tabelhandle en embedder cachen tussen zoekopdrachten.** De eerste kNN kost ~15 s
   (LanceDB leest van schijf), daarna ~0,1 s. Per vraag opnieuw `open_table()` doen gooit
   die opwarming telkens weg. Zelfde voor het rerankermodel.

## Werkcontract (standaard — Jeroen hoeft dit niet te typen)

- **Autonomie:** Voer omkeerbaar intern werk uit wanneer de intentie duidelijk is. Benoem materiële aannames. Vraag verduidelijking wanneer ambiguïteit het resultaat, een commitment, een privacygrens of de kosten verandert. Bestaande expliciete beperkingen voor externe acties blijven gelden.
- **Plan eerst:** plan substantieel werk en respecteer gevraagde review; gebruik alleen werkelijk beschikbare plan-tools.
3. **Verifieerbaar klaar.** Sluit af met een concrete done-check — tests/typecheck groen, smoke test, of screenshot — nooit "zou moeten werken". "Geen fouten!" betekent: draai de verificatie en toon het resultaat.
4. **UI-feedback gebatcht.** Zodra een UI-iteratie tweak-per-tweak dreigt te gaan: stel voor dat Jeroen zijn feedback als één genummerde batch (5–10 punten) mét slotcriterium aanlevert, en gebruik `/shot` (contact-sheet op 3 breakpoints) als visuele check in plaats van hem te laten doorklikken.
5. **Harde domeinregels documenteren.** Corrigeert Jeroen met een regel die altijd waar is ("X mag nooit Y")? Schrijf die meteen in het constraints-/rules-bestand van de repo, niet alleen in de fix.
6. **Beslissingen-brug.** Valt in deze repo een strategische beslissing (scope, architectuur, pricing, go/no-go)? Log ze in de cockpit: `node "D:\GithubLocal\J.A.R.V.I.S\decisions\log-decision.mjs" "<titel>" "<beslissing>" "<waarom>"` — AUTO-granted (trust-ledger), melden achteraf volstaat.
## Gedeeld werkcontract — Claude en Codex

- Voer omkeerbaar intern werk uit wanneer de intentie duidelijk is. Benoem materiële aannames. Vraag verduidelijking wanneer ambiguïteit het resultaat, een commitment, een privacygrens of de kosten verandert. Bestaande expliciete beperkingen voor externe acties blijven gelden.
- Plan substantieel werk eerst; respecteer expliciet gevraagde planreview vóór uitvoering.
- Verifieer wijzigingen met passende checks; meld wat wel en niet getest is.
- Behoud projectspecifieke domeinregels, privacygrenzen en externe toestemmingen.
- Bundel UI-feedback en gebruik de beschikbare visuele controle; veronderstel geen ontbrekende tools.
- AGENTS.md is de gedeelde bron; CLAUDE.md importeert die. Bewaar duurzame projectcontext in repo-documenten, niet uitsluitend in agent-chatgeheugen.
- Nieuwe repo: AGENTS.md met dit contract + CLAUDE.md met @AGENTS.md. Projectspecifieke skills in .agents/skills; voer agent-setup.ps1 uit voor de Claude-verwijzing.
- Bij gelijktijdig werk: controleer git-status, claim een afgebakende taak in een eigen docs/agent-work/<taak>.md wanneer werk over sessies loopt, en gebruik aparte worktrees voor overlappende codewijzigingen. Overschrijf nooit andermans wijzigingen. Lees vlak vóór een edit opnieuw; stop bij een onverwachte wijziging.
- Eindig een overdracht met huidige toestand, verificatie, open punten en volgende actie. Chatgeschiedenis wordt niet automatisch tussen agents gedeeld.

