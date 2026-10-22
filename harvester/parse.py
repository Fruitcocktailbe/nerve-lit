"""PubMed-efetch-XML omzetten naar platte dicts.

Stdlib ElementTree — de XML is groot maar simpel, en geen extra dependency.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")


def _text(element: ET.Element | None) -> str | None:
    """Alle tekst binnen een element, inclusief inline markup (<i>, <sup>, ...)."""
    if element is None:
        return None
    joined = "".join(element.itertext())
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined or None


def _parse_abstract(article: ET.Element) -> str | None:
    """Gestructureerde abstracts (BACKGROUND/METHODS/...) krijgen hun labels mee —
    dat helpt later bij sectie-aware chunking in fase 3."""
    node = article.find("Abstract")
    if node is None:
        return None
    parts: list[str] = []
    for chunk in node.findall("AbstractText"):
        body = _text(chunk)
        if not body:
            continue
        label = chunk.get("Label")
        parts.append(f"{label.strip().upper()}: {body}" if label else body)
    return "\n\n".join(parts) or None


def _parse_authors(article: ET.Element) -> list[str]:
    authors: list[str] = []
    for author in article.findall("./AuthorList/Author"):
        collective = _text(author.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        last = _text(author.find("LastName"))
        if not last:
            continue
        initials = _text(author.find("Initials"))
        authors.append(f"{last} {initials}" if initials else last)
    return authors


def _parse_mesh(citation: ET.Element) -> list[str]:
    """MeSH als 'Descriptor/qualifier' — de qualifier (bv. /surgery) draagt hier
    veel betekenis, die mag niet wegvallen."""
    terms: list[str] = []
    for heading in citation.findall("./MeshHeadingList/MeshHeading"):
        descriptor = _text(heading.find("DescriptorName"))
        if not descriptor:
            continue
        qualifiers = [q for q in (_text(x) for x in heading.findall("QualifierName")) if q]
        if qualifiers:
            terms.extend(f"{descriptor}/{qualifier}" for qualifier in qualifiers)
        else:
            terms.append(descriptor)
    return terms


def _parse_pub_date(article: ET.Element) -> tuple[int | None, str | None]:
    pub_date = article.find("./Journal/JournalIssue/PubDate")
    if pub_date is None:
        return None, None

    medline_date = _text(pub_date.find("MedlineDate"))
    if medline_date:
        # Vormen als "2019 Nov-Dec" of "1998-1999" — pak het eerste jaartal.
        match = YEAR_RE.search(medline_date)
        return (int(match.group(1)) if match else None), medline_date

    year = _text(pub_date.find("Year"))
    month = _text(pub_date.find("Month"))
    day = _text(pub_date.find("Day"))
    readable = " ".join(part for part in (year, month, day) if part) or None
    return (int(year) if year and year.isdigit() else None), readable


def _parse_ids(entry: ET.Element) -> dict[str, str]:
    ids: dict[str, str] = {}
    for article_id in entry.findall("./PubmedData/ArticleIdList/ArticleId"):
        id_type = article_id.get("IdType")
        value = _text(article_id)
        if id_type and value:
            ids[id_type.lower()] = value
    return ids


def parse_article(entry: ET.Element) -> dict[str, Any] | None:
    citation = entry.find("MedlineCitation")
    if citation is None:
        return None
    pmid = _text(citation.find("PMID"))
    article = citation.find("Article")
    if not pmid or article is None:
        return None

    ids = _parse_ids(entry)
    doi = ids.get("doi") or _text(
        article.find("./ELocationID[@EIdType='doi']")
    )
    pmcid = ids.get("pmc")
    year, pub_date = _parse_pub_date(article)

    return {
        "pmid": pmid,
        "doi": doi.lower() if doi else None,
        "pmcid": pmcid,
        "title": _text(article.find("ArticleTitle")),
        "abstract": _parse_abstract(article),
        "journal": _text(article.find("./Journal/Title")),
        "journal_iso": _text(article.find("./Journal/ISOAbbreviation")),
        "year": year,
        "pub_date": pub_date,
        "authors": _parse_authors(article),
        "mesh": _parse_mesh(citation),
        "keywords": [
            kw for kw in (_text(k) for k in citation.findall("./KeywordList/Keyword")) if kw
        ],
        "pubtypes": [
            pt
            for pt in (_text(p) for p in article.findall("./PublicationTypeList/PublicationType"))
            if pt
        ],
        "language": _text(article.find("Language")),
    }


def parse_efetch_xml(xml_bytes: bytes) -> list[dict[str, Any]]:
    """Alle artikels uit één efetch-respons. Boeken (PubmedBookArticle) worden
    overgeslagen — die zitten niet in onze scope-query."""
    root = ET.fromstring(xml_bytes)
    articles: list[dict[str, Any]] = []
    for entry in root.findall("PubmedArticle"):
        parsed = parse_article(entry)
        if parsed:
            articles.append(parsed)
    return articles
