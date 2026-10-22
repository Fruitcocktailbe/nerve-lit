"""Tests voor de PubMed-XML-parser."""

from harvester.parse import parse_efetch_xml

SAMPLE = b"""<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID Version="1">12345678</PMID>
   <Article>
    <Journal>
     <Title>The Journal of hand surgery</Title>
     <ISOAbbreviation>J Hand Surg Am</ISOAbbreviation>
     <JournalIssue><PubDate><Year>2019</Year><Month>Mar</Month></PubDate></JournalIssue>
    </Journal>
    <ArticleTitle>Nerve transfer for <i>elbow</i> flexion.</ArticleTitle>
    <Abstract>
     <AbstractText Label="BACKGROUND">We studied transfers.</AbstractText>
     <AbstractText Label="RESULTS">It worked.</AbstractText>
    </Abstract>
    <AuthorList>
     <Author><LastName>Poelaert</LastName><Initials>J</Initials></Author>
     <Author><CollectiveName>Study Group</CollectiveName></Author>
    </AuthorList>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
    <Language>eng</Language>
   </Article>
   <MeshHeadingList>
    <MeshHeading>
     <DescriptorName>Peripheral Nerves</DescriptorName>
     <QualifierName>surgery</QualifierName>
    </MeshHeading>
    <MeshHeading><DescriptorName>Humans</DescriptorName></MeshHeading>
   </MeshHeadingList>
   <KeywordList><Keyword>brachial plexus</Keyword></KeywordList>
  </MedlineCitation>
  <PubmedData>
   <ArticleIdList>
    <ArticleId IdType="pubmed">12345678</ArticleId>
    <ArticleId IdType="doi">10.1016/J.ABC.2019.01.002</ArticleId>
    <ArticleId IdType="pmc">PMC7654321</ArticleId>
   </ArticleIdList>
  </PubmedData>
 </PubmedArticle>
</PubmedArticleSet>
"""

MEDLINE_DATE = b"""<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID Version="1">999</PMID>
   <Article>
    <Journal><JournalIssue><PubDate>
      <MedlineDate>1998 Nov-Dec</MedlineDate>
    </PubDate></JournalIssue></Journal>
    <ArticleTitle>Old paper.</ArticleTitle>
   </Article>
  </MedlineCitation>
 </PubmedArticle>
</PubmedArticleSet>
"""


def test_parses_core_fields():
    (article,) = parse_efetch_xml(SAMPLE)
    assert article["pmid"] == "12345678"
    assert article["pmcid"] == "PMC7654321"
    assert article["year"] == 2019
    assert article["journal_iso"] == "J Hand Surg Am"
    assert article["language"] == "eng"


def test_doi_is_lowercased():
    # DOI's zijn case-insensitief; zonder normalisatie ontstaan dubbels.
    (article,) = parse_efetch_xml(SAMPLE)
    assert article["doi"] == "10.1016/j.abc.2019.01.002"


def test_title_keeps_inline_markup_text():
    (article,) = parse_efetch_xml(SAMPLE)
    assert article["title"] == "Nerve transfer for elbow flexion."


def test_structured_abstract_keeps_labels():
    (article,) = parse_efetch_xml(SAMPLE)
    assert "BACKGROUND: We studied transfers." in article["abstract"]
    assert "RESULTS: It worked." in article["abstract"]


def test_mesh_keeps_qualifiers():
    # De qualifier draagt hier de betekenis: /surgery mag niet wegvallen.
    (article,) = parse_efetch_xml(SAMPLE)
    assert "Peripheral Nerves/surgery" in article["mesh"]
    assert "Humans" in article["mesh"]


def test_collective_author_is_kept():
    (article,) = parse_efetch_xml(SAMPLE)
    assert article["authors"] == ["Poelaert J", "Study Group"]


def test_medline_date_yields_year():
    (article,) = parse_efetch_xml(MEDLINE_DATE)
    assert article["year"] == 1998


def test_empty_set_is_not_an_error():
    assert parse_efetch_xml(b"<PubmedArticleSet></PubmedArticleSet>") == []


def test_status_export_writes_all_rows(tmp_path):
    """De status-CSV moet één rij per artikel geven, met de afgeleide kolommen."""
    import csv as _csv
    import sqlite3

    from harvester import db, report

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db.upsert_articles(conn, parse_efetch_xml(SAMPLE))
    # Eén artikel indexeren zonder full-text -> searchable_on = 'abstract'.
    conn.execute("UPDATE articles SET index_status='indexed' WHERE pmid='12345678'")
    conn.commit()

    class _Cfg:
        data_dir = tmp_path

    out = report.export_status(conn, _Cfg(), tmp_path / "status.csv")
    with out.open(encoding="utf-8-sig") as handle:
        rows = list(_csv.DictReader(handle, delimiter=";"))

    assert len(rows) == 1
    row = rows[0]
    assert row["pmid"] == "12345678"
    assert row["has_abstract"] == "yes"
    assert row["searchable_on"] == "abstract"
    assert row["pubmed_url"].endswith("/12345678/")
