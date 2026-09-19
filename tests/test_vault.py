"""Vault node: long notes come back as an outline, sections read on their own."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from sommus.nodes.vault import server as vault


@pytest.fixture
def notes(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT", tmp_path)
    filler = "x" * 80 + "\n"
    (tmp_path / "TODO.md").write_text(
        "---\ntype: todo\n---\n\n# TODO\n\nIntro line.\n\n"
        "## Weekend\n- [ ] ECE 190 video\n" + filler * 40 + "### Saturday\n- [ ] gym\n"
        "## This week\n- [ ] MATH 115 tutorial\n" + filler * 40 + "```\n## not a heading\n```\n"
        "## Done\n- old stuff\n"
    )
    (tmp_path / "Short.md").write_text("# Short\n\n## Weekend\nOnly a little.\n")
    return tmp_path


def test_long_note_returns_outline_not_body(notes):
    out = vault.read_note("TODO.md")
    assert "outline" in out and 'section="' in out
    assert "- Weekend" in out and "  - Saturday" in out and "- Done" in out
    assert "ECE 190 video" not in out  # the body stays out until asked for
    assert "not a heading" not in out  # headings inside code fences are ignored
    assert len(out) < 1000


def test_section_returns_heading_and_subheadings_only(notes):
    out = vault.read_note("TODO.md", section="weekend")
    assert out.startswith("## Weekend")
    assert "ECE 190 video" in out and "### Saturday" in out and "gym" in out
    assert "MATH 115" not in out


def test_section_all_returns_everything(notes):
    out = vault.read_note("TODO.md", section="all")
    assert "ECE 190 video" in out and "MATH 115 tutorial" in out and "old stuff" in out


def test_short_note_is_returned_whole(notes):
    assert "Only a little." in vault.read_note("Short.md")


def test_missing_section_lists_headings(notes):
    with pytest.raises(vault.VaultError, match="Headings: TODO, Weekend"):
        vault.read_note("TODO.md", section="exams")


def test_search_names_the_heading_of_each_hit(notes):
    out = vault.search_vault("gym")
    assert "TODO.md:" in out and "[Saturday]" in out


def test_paths_stay_inside_the_vault(notes):
    with pytest.raises(vault.VaultError, match="outside the vault"):
        vault.read_note("../secret.md")


def test_tool_errors_reach_the_model_as_tool_errors(notes):
    # The MCP-facing wrapper turns VaultError into ToolError with the same message.
    registered = vault.server._tool_manager.get_tool("read_note") if hasattr(vault.server, "_tool_manager") else None
    if registered is None:
        pytest.skip("tool registry layout differs in this MCP SDK version")
    with pytest.raises(ToolError):
        registered.fn(path="TODO.md", section="exams")
