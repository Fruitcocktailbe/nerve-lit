"""Tests voor chunking en ruisfilters.

De ruisfilters zijn het soort code dat stil kapot gaat: als ze te veel weggooien
mist de index inhoud, als ze te weinig weggooien vullen referentielijsten de
zoekresultaten. Allebei zonder foutmelding.
"""

from index.extract import (
    CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    abstract_to_chunks,
    is_noise_chunk,
    jats_to_chunks,
    looks_like_cover_page,
    looks_like_licence,
    looks_like_references,
    pages_to_chunks,
    strip_references,
)

CLINICAL_TEXT = (
    "The anterior interosseous nerve was transferred to the ulnar motor fascicle "
    "in a supercharged end-to-side fashion. Grip strength improved in the majority "
    "of patients at twelve months of follow-up, and intrinsic muscle bulk recovered "
    "in the severe compression group. No donor site morbidity was observed. "
)

REFERENCE_LIST = (
    "1. Merrell GA, Barrie KA (2001) Results of nerve transfer techniques. J Hand Surg 26:303-314. "
    "2. Ozkan T, Ozer K (2001) Restoration of sensibility. J Hand Surg 26:44-51. "
    "3. Mackinnon SE (2015) Nerve transfers. Plast Reconstr Surg 136:1-12. "
    "doi.org/10.1097/PRS.0000000000001514 "
    "4. Brown JM (2009) Distal nerve transfers. Neurosurg Focus 26:E12-E20. "
)

LICENCE_BLOCK = (
    "This article is licensed under a Creative Commons Attribution 4.0 International "
    "License, which permits use, sharing, adaptation and reproduction in any medium "
    "or format, as long as you give appropriate credit to the original author. "
    "See http://creativecommons.org/licenses/by/4.0/ for details. "
)

COVER_PAGE = (
    "Research Information Portal. Document Version: Publisher's PDF. "
    "Link to publication. Terms and Conditions of Use. General rights. "
    "Citation for the published version (APA): Burahee A. et al. "
)


def test_clinical_text_is_not_noise():
    assert not is_noise_chunk(CLINICAL_TEXT * 3)


def test_reference_list_is_noise():
    assert looks_like_references(REFERENCE_LIST * 2)
    assert is_noise_chunk(REFERENCE_LIST * 2)


def test_licence_block_is_noise():
    assert looks_like_licence(LICENCE_BLOCK)
    assert is_noise_chunk(LICENCE_BLOCK)


def test_cover_page_is_noise():
    assert looks_like_cover_page(COVER_PAGE)


def test_strip_references_cuts_the_tail():
    document = CLINICAL_TEXT * 6 + "\nReferences\n" + REFERENCE_LIST
    stripped = strip_references(document)
    assert "Merrell" not in stripped
    assert "supercharged end-to-side" in stripped


def test_strip_references_ignores_an_early_mention():
    # 'References' in de eerste helft is een kopje, geen bibliografie.
    document = "Intro\nReferences\n" + CLINICAL_TEXT * 20
    assert strip_references(document) == document


def test_chunks_carry_page_numbers():
    pages = [CLINICAL_TEXT * 4, CLINICAL_TEXT * 4, CLINICAL_TEXT * 4]
    chunks = pages_to_chunks(pages)
    assert chunks
    assert all(chunk.page_start >= 1 for chunk in chunks)
    assert max(chunk.page_end for chunk in chunks) > 1


def test_chunk_indices_are_contiguous():
    chunks = pages_to_chunks([CLINICAL_TEXT * 12])
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_short_document_yields_nothing():
    assert pages_to_chunks(["te kort"]) == []


def test_normal_abstract_stays_one_chunk():
    chunks = abstract_to_chunks(CLINICAL_TEXT)
    assert len(chunks) == 1


def test_long_abstract_gets_split():
    # Gestructureerde trial-abstracts halen makkelijk 4000+ tekens; als één
    # vector zouden ze te vaag worden om nog iets terug te vinden.
    long_abstract = CLINICAL_TEXT * 20
    assert len(long_abstract) > CHUNK_CHARS
    chunks = abstract_to_chunks(long_abstract)
    assert len(chunks) > 1
    assert all(len(chunk.text) <= CHUNK_CHARS for chunk in chunks)


def test_too_short_abstract_yields_nothing():
    assert abstract_to_chunks("kort") == []


JATS = f"""<article><body>
 <sec><title>Introduction</title><p>{CLINICAL_TEXT * 2}</p></sec>
 <sec><title>Methods</title><p>{CLINICAL_TEXT * 2}</p>
   <sec><title>Statistics</title><p>{CLINICAL_TEXT * 2}</p></sec>
 </sec>
 <sec><title>References</title><p>{REFERENCE_LIST}</p></sec>
</body></article>"""


def test_jats_sections_become_chunks_with_headings(tmp_path):
    path = tmp_path / "article.xml"
    path.write_text(JATS, encoding="utf-8")
    chunks = jats_to_chunks(path)

    texts = [chunk.text for chunk in chunks]
    assert any(text.startswith("Introduction") for text in texts)
    # Subsecties erven het pad van hun ouder, zodat een citaat context houdt.
    assert any("Methods > Statistics" in text for text in texts)
    # De referentiesectie mag er niet in zitten.
    assert not any("Merrell" in text for text in texts)
    assert all(len(chunk.text) >= MIN_CHUNK_CHARS for chunk in chunks)


def test_jats_heading_is_not_doubled(tmp_path):
    # De kop wordt vooraan gezet én stond in de <title> — die mag niet dubbel.
    path = tmp_path / "article.xml"
    path.write_text(JATS, encoding="utf-8")
    intro = next(c.text for c in jats_to_chunks(path) if c.text.startswith("Introduction"))
    # Na de kop + witregel begint de body; "Introduction" mag daar niet weer staan.
    body = intro.split("\n\n", 1)[1]
    assert not body.lstrip().startswith("Introduction")


def test_jats_long_section_heading_not_doubled(tmp_path):
    # Een lange sectie wordt gesplitst; élk stuk krijgt de kop maar één keer.
    long_body = "".join(f"<p>{CLINICAL_TEXT * 3}</p>" for _ in range(6))
    xml = f"<article><body><sec><title>Discussion</title>{long_body}</sec></body></article>"
    path = tmp_path / "long.xml"
    path.write_text(xml, encoding="utf-8")

    chunks = jats_to_chunks(path)
    assert len(chunks) > 1  # daadwerkelijk gesplitst
    for chunk in chunks:
        assert chunk.text.startswith("Discussion")
        assert not chunk.text.split("\n\n", 1)[1].lstrip().startswith("Discussion")


JATS_WITH_FIGURE = f"""<article><body>
 <sec><title>Results</title>
   <p>{CLINICAL_TEXT * 2}</p>
   <fig id="f1">
     <label>Fig. 1</label>
     <caption><p>MRC grade M4 recovery achieved in 82 percent at twelve months.</p></caption>
     <graphic xlink:href="fig1.jpg" xmlns:xlink="http://www.w3.org/1999/xlink"/>
   </fig>
   <table-wrap id="t1">
     <label>Table 1</label>
     <caption><p>Baseline demographics of the supercharged transfer cohort.</p></caption>
     <table><tr><td>Age</td><td>42.7</td></tr><tr><td>Sex</td><td>M</td></tr></table>
   </table-wrap>
 </sec>
</body></article>"""


def test_jats_keeps_figure_and_table_captions(tmp_path):
    path = tmp_path / "figs.xml"
    path.write_text(JATS_WITH_FIGURE, encoding="utf-8")
    all_text = " ".join(chunk.text for chunk in jats_to_chunks(path))

    # Bijschriften — het klinisch interessante deel — moeten erin zitten.
    assert "MRC grade M4 recovery achieved in 82 percent" in all_text
    assert "Baseline demographics of the supercharged transfer cohort" in all_text
    # De ruwe tabelcellen en beeldverwijzing niet.
    assert "42.7" not in all_text
    assert "fig1.jpg" not in all_text
