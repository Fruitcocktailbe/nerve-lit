"""De scope-query voor het corpus.

Gekozen 2026-07-22 (PLAN.md fase 1): het hele veld peripheral nerve surgery,
Engels + humaan, geen tijdslimiet. ~41k artikels.

Verander deze query NIET zonder de beslissing te loggen — de metadata-store en
de index zijn eraan opgehangen (query_hash is de sleutel van de harvest-queue).
"""

SCOPE_QUERY = """
("Peripheral Nerves/surgery"[Mesh] OR "Peripheral Nerve Injuries/surgery"[Mesh]
 OR "Nerve Compression Syndromes/surgery"[Mesh] OR "nerve transfer"[tiab]
 OR "nerve graft*"[tiab] OR neurotization[tiab] OR neurorrhaphy[tiab]
 OR "nerve repair"[tiab] OR "nerve reconstruction"[tiab] OR "brachial plexus"[tiab]
 OR "regenerative peripheral nerve interface"[tiab] OR RPNI[tiab]
 OR "targeted muscle reinnervation"[tiab]
 OR ("facial nerve"[tiab] AND (reanimation OR reconstruction OR palsy OR grafting))
 OR "nerve decompression"[tiab] OR "nerve entrapment"[tiab])
AND English[lang] AND humans[mesh]
"""

# Kleinere subsets voor dry-runs en smoke tests.
NAMED_QUERIES = {
    "scope": SCOPE_QUERY,
    # De reconstructieve kern — handig om klein te starten.
    "core": '("nerve transfer"[tiab] OR RPNI[tiab] OR "targeted muscle reinnervation"[tiab]) '
    "AND English[lang] AND humans[mesh]",
    # Enkel reviews uit de volledige scope (~5k).
    "reviews": f"({SCOPE_QUERY}) AND review[pt]",
    # Enkel wat gratis full-text heeft (~8k) — de auto-downloadbare set.
    "freefulltext": f'({SCOPE_QUERY}) AND "free full text"[filter]',
}


def resolve(name_or_query: str) -> str:
    """Een named query of een letterlijke PubMed-query terug als string."""
    return " ".join(NAMED_QUERIES.get(name_or_query, name_or_query).split())
