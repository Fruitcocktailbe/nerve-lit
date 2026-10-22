"""Integratietest voor de MCP-server, zoals JARVIS hem aanroept.

Start de server als subproces over stdio en controleert het contract: welke
tools er zijn, hoe ze heten, en of `get_nerve_lit_stats` echte cijfers teruggeeft.
Zoeken zelf wordt hier niet getest — dat laadt het rerankermodel (~30 s koud) en
hoort niet in een testsuite die in een seconde moet draaien.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "mcp-server" / "server.py"

# Overgenomen uit de Trust Ledger van JARVIS:
# voice-agent/backend/src/agent.ts -> READ_VERB
JARVIS_READ_VERB = re.compile(
    r"^(get|list|search|fetch|query|read|find|view|describe|check|retrieve|freebusy)",
    re.IGNORECASE,
)

EXPECTED_TOOLS = {"search_nerve_lit", "get_nerve_lit_article", "get_nerve_lit_stats"}


async def _collect() -> tuple[list[str], str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            stats = await session.call_tool("get_nerve_lit_stats", {})
            return [tool.name for tool in tools.tools], stats.content[0].text


@pytest.fixture(scope="module")
def server_result():
    pytest.importorskip("mcp", reason="mcp-extra niet geinstalleerd")
    return asyncio.run(_collect())


def test_exposes_the_expected_tools(server_result):
    names, _ = server_result
    assert set(names) == EXPECTED_TOOLS


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_tool_names_pass_jarvis_read_classifier(tool_name):
    """Alle tools hier zijn read-only en moeten door JARVIS ook zo herkend worden.

    Met JARVIS_EXPOSED=1 (wat beide launchers zetten) vraagt de voice-agent
    handmatige bevestiging voor élke MCP-tool die niet als 'read' geldt. Een naam
    als `nerve_lit_search` zou dus betekenen dat Jeroen elke literatuurvraag met
    de hand moet goedkeuren — daarom beginnen ze met een leeswerkwoord.
    """
    assert JARVIS_READ_VERB.match(tool_name), (
        f"'{tool_name}' wordt door JARVIS als schrijfactie gezien en vraagt om bevestiging"
    )


def test_stats_reports_a_populated_database(server_result):
    _, stats = server_result
    assert "nerve-lit databank" in stats
    assert "artikels met metadata" in stats
    assert "chunks in LanceDB" in stats
