"""PDF naar tekstchunks.

PyMuPDF voor de extractie (snel, geen externe service). Chunkt op tekenlengte in
plaats van tokens: bge-m3 haalt ~4 tekens per token voor Engelse tekst, en een
exacte tokenizer erbij halen kost meer dan de precisie hier waard is.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

# ~650 tokens per chunk bij ~4 tekens/token, met ~20% overlap.
CHUNK_CHARS = 2600
OVERLAP_CHARS = 500
MIN_CHUNK_CHARS = 200

# Kopteksten die in vrijwel elk artikel terugkomen en niets toevoegen.
NOISE_PATTERNS = [
    re.compile(r"Downloaded from .*? on \d.*", re.IGNORECASE),
    re.compile(r"This article is protected by copyright.*", re.IGNORECASE),
    re.compile(r"^\s*Page \d+ of \d+\s*$", re.MULTILINE),
]


@dataclass
class Chunk:
    text: str
    index: int
    page_start: int
    page_end: int


def clean_text(text: str) -> str:
    for pattern in NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    # Woordafbrekingen aan regeleindes herstellen: "recon-\nstruction" -> "reconstruction".
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


REFERENCES_HEADING = re.compile(
    r"\n[ \t]*(?:\d+[.)]?[ \t]*)?(references|bibliography|literature cited)[ \t]*\n",
    re.IGNORECASE,
)
# Een referentielijst verraadt zich door de dichtheid van jaartallen tussen
# haakjes, DOI's en paginabereiken — niet door één enkel kenmerk.
REFERENCE_MARKERS = (
    re.compile(r"\(\d{4}\)"),
    re.compile(r"doi\.org/|doi:", re.IGNORECASE),
    re.compile(r"\b\d+\s*[:–-]\s*\d+\b"),
)


def strip_references(text: str) -> str:
    """Kap de bibliografie eraf.

    Zonder dit vult de index zich met referentielijsten: die staan vol
    vaktermen, dus ze scoren hoog op elke zoekopdracht terwijl ze niets
    beantwoorden. Alleen knippen in de laatste helft van het document — anders
    sneuvelt een artikel dat 'References' in een kopje vroeg vermeldt.
    """
    matches = list(REFERENCES_HEADING.finditer(text))
    if not matches:
        return text
    cutoff = len(text) // 2
    late = [m for m in matches if m.start() >= cutoff]
    return text[: late[-1].start()] if late else text


def looks_like_references(text: str) -> bool:
    """Vangt de resten op die strip_references miste (bv. een lijst zonder kop)."""
    if len(text) < MIN_CHUNK_CHARS:
        return False
    density = sum(len(pattern.findall(text)) for pattern in REFERENCE_MARKERS)
    return density / (len(text) / 1000) > 12


# Licentieblokken staan in elk OA-artikel en zijn onderling bijna identiek. Ze
# zeggen niets over zenuwchirurgie, maar wél veel woorden — dus zonder filter
# duiken ze op bij zoekopdrachten.
LICENCE_MARKERS = (
    "creative commons",
    "creativecommons.org",
    "licensed under",
    "permits use, sharing",
    "permits any non-commercial use",
    "reproduction in any medium",
    "all rights reserved",
    "give appropriate credit",
)


def looks_like_licence(text: str) -> bool:
    lowered = text.lower()
    return sum(marker in lowered for marker in LICENCE_MARKERS) >= 2


def is_noise_chunk(text: str) -> bool:
    """Eén poort voor alles wat wel tekst is maar geen inhoud: referentielijsten,
    licentieblokken en repository-dekbladen."""
    return looks_like_references(text) or looks_like_licence(text) or looks_like_cover_page(text)


# Repositories (EUR, universiteitsbibliotheken) plakken een dekblad vóór het
# artikel. Dat blad staat vol titel- en auteurstermen, dus het scoort hoog op
# zoekopdrachten terwijl het niets inhoudelijks bevat.
COVER_PAGE_MARKERS = (
    "link to publication",
    "terms and conditions of use",
    "citation for the published version",
    "document version",
    "publication status and date",
    "research information portal",
    "general rights",
    "take down policy",
    "downloaded from",
    "publisher's pdf",
)


def looks_like_cover_page(text: str) -> bool:
    lowered = text.lower()
    return sum(marker in lowered for marker in COVER_PAGE_MARKERS) >= 3


def drop_cover_pages(pages: list[str]) -> list[str]:
    """Alleen de eerste twee pagina's testen: verderop in een artikel kunnen
    dezelfde woorden legitiem voorkomen."""
    result = list(pages)
    while result and len(result) > 1 and looks_like_cover_page(result[0]):
        result.pop(0)
    return result


def pdf_to_pages(pdf_path: Path) -> list[str]:
    """Tekst per pagina. Lege lijst als de PDF onleesbaar is (scan zonder OCR)."""
    with fitz.open(pdf_path) as document:
        return [clean_text(page.get_text("text")) for page in document]


def pages_to_chunks(pages: list[str]) -> list[Chunk]:
    """Chunkt over paginagrenzen heen, maar onthoudt op welke pagina's een chunk
    staat — dat is wat een citaat later bruikbaar maakt."""
    if not pages:
        return []

    # Eén lange string plus een index van waar elke pagina begint.
    offsets: list[tuple[int, int]] = []  # (startpositie, paginanummer, 1-based)
    buffer: list[str] = []
    cursor = 0
    for page_number, page_text in enumerate(pages, start=1):
        if not page_text:
            continue
        offsets.append((cursor, page_number))
        buffer.append(page_text)
        cursor += len(page_text) + 1
    full_text = strip_references("\n".join(buffer))
    if len(full_text) < MIN_CHUNK_CHARS:
        return []

    def page_at(position: int) -> int:
        page = offsets[0][1] if offsets else 1
        for start, number in offsets:
            if start <= position:
                page = number
            else:
                break
        return page

    chunks: list[Chunk] = []
    start = 0
    step = CHUNK_CHARS - OVERLAP_CHARS
    while start < len(full_text):
        end = min(start + CHUNK_CHARS, len(full_text))
        if end < len(full_text):
            # Liever afbreken op een zinseinde dan midden in een woord.
            window = full_text.rfind(". ", start + step // 2, end)
            if window != -1:
                end = window + 1
        piece = full_text[start:end].strip()
        if len(piece) >= MIN_CHUNK_CHARS and not is_noise_chunk(piece):
            chunks.append(
                Chunk(
                    text=piece,
                    index=len(chunks),
                    page_start=page_at(start),
                    page_end=page_at(max(start, end - 1)),
                )
            )
        if end >= len(full_text):
            break
        start = max(end - OVERLAP_CHARS, start + 1)

    return chunks


# Ruis in JATS die niets bijdraagt aan een semantische zoekopdracht. Let op:
# fig/table-wrap staan hier NIET bij — daar plukken we het bijschrift uit (zie
# _caption_text). De ruwe tabelcellen (<table>) en beeldverwijzing (<graphic>)
# gaan wel weg: cellen extraheren als losse getallen, beelden zijn niet in de XML.
JATS_DROP_TAGS = {"ref-list", "back", "table", "xref", "graphic", "disp-formula"}
JATS_CAPTION_TAGS = {"fig", "table-wrap"}
JATS_DROP_SECTIONS = re.compile(
    r"^(references?|acknowledg|author contribution|conflict|funding|"
    r"supplementary|competing interest|abbreviation)",
    re.IGNORECASE,
)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _caption_text(element: ET.Element) -> str:
    """Label + bijschrift van een <fig> of <table-wrap>, zonder de tabelcellen.

    Bijschriften zijn vaak de enige plek waar een concreet resultaat staat
    ('Fig 3: M4-herstel bij 82% na 12 maanden'), dus die horen wél in de index —
    de afbeelding en de ruwe cellen niet."""
    pieces: list[str] = []
    for child in element:
        tag = _local(child.tag)
        if tag == "label":
            pieces.append(" ".join(child.itertext()))
        elif tag == "caption":
            pieces.append(" ".join(child.itertext()))
    return clean_text(" ".join(pieces))


def _jats_section_text(section: ET.Element) -> str:
    """Tekst van één <sec>: prose + figuur-/tabelbijschriften, zonder de ruwe
    tabellen, beelden en referentiemarkers."""
    parts: list[str] = []
    for child in section:
        tag = _local(child.tag)
        if tag == "sec":
            continue  # subsecties worden apart afgelopen
        if tag == "title":
            continue  # de kop wordt door jats_to_chunks al vooraan gezet
        if tag in JATS_CAPTION_TAGS:
            caption = _caption_text(child)
            if caption:
                parts.append(caption)
            continue
        if tag in JATS_DROP_TAGS:
            continue
        parts.append(" ".join(child.itertext()))
    return clean_text(" ".join(parts))


def jats_to_chunks(xml_path: Path) -> list[Chunk]:
    """PMC JATS-XML naar chunks, één per sectie.

    Secties zijn hier al de natuurlijke eenheid — de auteur heeft het document
    zelf opgedeeld. Dat is beter dan blind op tekenlengte knippen, dus alleen
    te lange secties worden verder gesplitst.
    """
    root = ET.parse(xml_path).getroot()
    body = root.find(".//body")
    if body is None:
        return []

    chunks: list[Chunk] = []

    def walk(node: ET.Element, heading: str) -> None:
        title_node = node.find("title")
        title = " ".join(title_node.itertext()).strip() if title_node is not None else ""
        full_heading = f"{heading} > {title}" if heading and title else (title or heading)

        if title and JATS_DROP_SECTIONS.match(title):
            return

        text = _jats_section_text(node)
        if len(text) >= MIN_CHUNK_CHARS and not is_noise_chunk(text):
            def _labelled(body: str) -> str:
                return f"{full_heading}\n\n{body}" if full_heading else body

            if len(_labelled(text)) <= CHUNK_CHARS * 1.4:
                chunks.append(
                    Chunk(text=_labelled(text), index=len(chunks), page_start=0, page_end=0)
                )
            else:
                # Splitsen op de ruwe tekst, dan pas de kop per stuk vooraan —
                # anders krijgt elk stuk de kop twee keer.
                for piece in pages_to_chunks([text]):
                    chunks.append(
                        Chunk(
                            text=_labelled(piece.text),
                            index=len(chunks),
                            page_start=0,
                            page_end=0,
                        )
                    )

        for subsection in node.findall("sec"):
            walk(subsection, full_heading)

    sections = body.findall("sec")
    if sections:
        for section in sections:
            walk(section, "")
        # Figuren/tabellen die los in <body> zweven (niet in een sec) worden door
        # de sectie-walk gemist — hun bijschriften alsnog oppikken.
        floating = [
            _caption_text(child)
            for child in body
            if _local(child.tag) in JATS_CAPTION_TAGS
        ]
        floating_text = clean_text(" ".join(c for c in floating if c))
        if len(floating_text) >= MIN_CHUNK_CHARS:
            for piece in pages_to_chunks([floating_text]):
                chunks.append(
                    Chunk(
                        text=f"Figures\n\n{piece.text}",
                        index=len(chunks),
                        page_start=0,
                        page_end=0,
                    )
                )
    else:
        # Sommige artikels (case reports, editorials) hebben geen <sec>-opdeling.
        chunks = pages_to_chunks([clean_text(" ".join(body.itertext()))])

    return chunks


def abstract_to_chunks(abstract: str) -> list[Chunk]:
    """Fallback voor artikels zonder full-text: het abstract is nog altijd
    doorzoekbaar. ~33k van de 41k artikels hebben geen OA-full-text, dus dit
    draagt het grootste deel van het corpus."""
    text = clean_text(abstract or "")
    if len(text) < MIN_CHUNK_CHARS:
        return []
    if len(text) <= CHUNK_CHARS:
        return [Chunk(text=text, index=0, page_start=0, page_end=0)]
    # ~2% van de abstracts is langer dan een chunk (gestructureerde abstracts van
    # trials). Die opsplitsen houdt de embeddings scherp in plaats van één vector
    # over vijf secties uit te smeren.
    return pages_to_chunks([text])


def extract_chunks(
    fulltext_path: Path | None, abstract: str | None
) -> tuple[list[Chunk], str]:
    """Chunks plus de bron ('jats', 'pdf' of 'abstract') waar ze vandaan komen.

    Valt altijd terug op het abstract: liever een artikel dat vindbaar is op zijn
    samenvatting dan een gat in de index.
    """
    if fulltext_path and fulltext_path.exists():
        suffix = fulltext_path.suffix.lower()
        try:
            if suffix in (".xml", ".nxml"):
                chunks = jats_to_chunks(fulltext_path)
                if chunks:
                    return chunks, "jats"
            elif suffix == ".pdf":
                chunks = pages_to_chunks(drop_cover_pages(pdf_to_pages(fulltext_path)))
                if chunks:
                    return chunks, "pdf"
        except Exception:  # noqa: BLE001 - kapot bestand mag niet fataal zijn
            pass
    return abstract_to_chunks(abstract or ""), "abstract"
