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


def test_new_todos_land_in_the_inbox_above_done_and_are_read_back(tmp_path):
    """They used to be appended under the Done table, where nothing ever read them."""
    from sommus.brain import campus
    from sommus.nodes.vault.server import with_inbox_item

    note = "# TODO\n\n## This week\n- [ ] gym\n\n## Later\n- [ ] taxes\n\n## Done\n\n| Date | What |\n"
    once = with_inbox_item(note, "- [ ] buy milk *(added 2026-09-28)*")
    twice = with_inbox_item(once, "- [ ] call Didi *(added 2026-09-28)*")

    assert twice.index("## 📥 Inbox") < twice.index("## Done")
    assert twice.index("buy milk") < twice.index("call Didi") < twice.index("## Done")
    path = tmp_path / "TODO.md"
    path.write_text(twice)
    assert campus.task_sections(path) == (["gym"], ["buy milk", "call Didi"])


def test_a_phrase_found_nowhere_falls_back_to_notes_with_every_word(notes):
    (notes / "MATH 115.md").write_text("# MATH 115\n\n## Tests\nMidterm: Mon Oct 26, 8 PM\n")
    exact = vault.search_vault("Mon Oct 26")
    assert "MATH 115.md:4" in exact
    loose = vault.search_vault("math 115 midterm")
    assert "all of its words" in loose and "MATH 115.md:4" in loose
    assert "Nothing in the vault" in vault.search_vault("linear midterm quantum")


def test_forget_reaches_the_diary_when_no_fact_matches_and_recall_reads_it(tmp_path, monkeypatch):
    from datetime import date

    from sommus.brain import diary, memory

    monkeypatch.setattr(vault, "MEMORY", tmp_path / "Memory.md")
    monkeypatch.setattr(vault, "DIARY", tmp_path / "Diary.md")
    memory.remember(vault.MEMORY, "Gym days are Monday and Friday")
    diary.save(vault.DIARY, [diary.Entry(date(2026, 10, 12), "He had his Amazon interview. He cooked dal.")])
    assert "Amazon interview" in vault.recall_diary("2026-10-12")
    assert vault.forget("gym days").startswith("Forgot: Gym days")  # a fact first
    assert vault.forget("amazon") == "Forgot from the diary: He had his Amazon interview."
    assert "Amazon" not in vault.recall_diary("")
    with pytest.raises(vault.VaultError, match="Nothing remembered"):
        vault.forget("skydiving")


def test_a_git_that_hangs_is_stopped_with_a_message(notes, monkeypatch):
    import subprocess

    (notes / ".git").mkdir()

    def hang(*args, **kwargs):
        assert kwargs.get("timeout"), "every git call needs a timeout"
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(vault.subprocess, "run", hang)
    with pytest.raises(vault.VaultError, match="took over 30 s"):
        vault.commit_vault("notes")


def test_list_notes_for_a_folder_skips_siblings_that_share_its_name_prefix(notes):
    """Issue #5: "Career" also listed "Career Fair/" and "Career2/" because the check was a string prefix."""
    for folder in ("Career", "Career Fair", "Career2"):
        (notes / folder).mkdir()
        (notes / folder / "a.md").write_text("# a\n")
    out = vault.list_notes("Career")
    assert out.startswith("1 notes:") and "Career/a.md" in out
    assert "Career Fair" not in out and "Career2" not in out
    assert vault.list_notes().startswith("5 notes:")  # whole vault: TODO, Short and one note per folder


def test_archived_notes_come_last_in_a_search(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT", tmp_path)
    (tmp_path / "Nutrition" / "Archive").mkdir(parents=True)
    (tmp_path / "Nutrition" / "Archive" / "Handoff.md").write_text("Drizzilicious 1 bag\n")
    (tmp_path / "Nutrition" / "Labels.md").write_text("Drizzilicious label 90 cal\n")
    hits = vault.search_vault("drizzilicious").splitlines()
    assert hits[1].startswith("Nutrition/Labels.md") and hits[2].startswith("Nutrition/Archive/")
