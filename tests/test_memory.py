"""Long-term memory: facts saved by Sommus, loaded into the prompt, never secrets."""

from dataclasses import replace
from datetime import date

import pytest
from fakes import FakeModel, build_node, config, text_reply
from mcp import Client

from sommus.brain import memory
from sommus.brain.loop import Brain
from sommus.brain.nodes import NodeHub
from sommus.brain.permissions import Policy
from sommus.brain.store import Store


def test_remember_forget_round_trip(tmp_path):
    path = tmp_path / "Memory.md"
    assert memory.remember(path, "Gym days are Monday, Wednesday and Friday.", date(2026, 9, 19)) == (
        "Remembered: Gym days are Monday, Wednesday and Friday."
    )
    memory.remember(path, "Prefers Spotify over Apple Music")
    assert "*(2026-09-19)*" in path.read_text()
    assert memory.facts(path) == ["Gym days are Monday, Wednesday and Friday", "Prefers Spotify over Apple Music"]
    assert memory.remember(path, "prefers spotify over apple music").startswith("Already remembered")
    assert memory.forget(path, "gym days") == "Forgot: Gym days are Monday, Wednesday and Friday."
    assert memory.facts(path) == ["Prefers Spotify over Apple Music"]


def test_hand_edits_survive(tmp_path):
    path = tmp_path / "Memory.md"
    path.write_text("# Sommus Memory\n\n- Written by hand\nnot a fact line\n")
    memory.remember(path, "Added later")
    assert memory.facts(path) == ["Written by hand", "Added later"]


@pytest.mark.parametrize(
    "fact",
    ["My bank password is hunter2", "Sommus PIN is 4821", "the API key starts with sk-", "2FA backup code 1234"],
)
def test_secrets_are_never_stored(tmp_path, fact):
    with pytest.raises(memory.MemoryError, match="secret"):
        memory.remember(tmp_path / "Memory.md", fact)
    assert not (tmp_path / "Memory.md").exists()


def test_forget_refuses_vague_or_missing(tmp_path):
    path = tmp_path / "Memory.md"
    for n in range(5):
        memory.remember(path, f"Class fact number {n}")
    with pytest.raises(memory.MemoryError, match="more specific"):
        memory.forget(path, "class fact")
    with pytest.raises(memory.MemoryError, match="No remembered fact"):
        memory.forget(path, "dentist")


def test_prompt_block_lists_facts(tmp_path):
    path = tmp_path / "Memory.md"
    assert memory.prompt_block(path, "Jashan") == ""
    memory.remember(path, "Sleeps around 1 AM")
    assert (
        memory.prompt_block(path, "Jashan") == "\n\n--- What Jashan has told you to remember ---\n- Sleeps around 1 AM"
    )


async def test_brain_reloads_prompt_only_when_memory_changes(tmp_path):
    path = tmp_path / "Memory.md"
    memory.remember(path, "Gym days are Monday, Wednesday and Friday")
    cfg = replace(config(tmp_path), memory_path=path)
    node, _ = build_node()
    async with NodeHub((), Policy({})) as hub:
        await hub.add("test", Client(node))
        model = FakeModel(text_reply("Hi."), text_reply("Hi."), text_reply("Hi."))
        brain = Brain(cfg, hub, Store(tmp_path / "t.db"), client=model)
        assert "Gym days" in brain.system and "`remember`" in brain.system

        before = brain.system
        [e async for e in brain.handle("hello", None)]
        assert brain.system is before  # unchanged file: same prompt, cache stays warm

        memory.remember(path, "Prefers dark mode")
        [e async for e in brain.handle("hello", None)]
        assert "Prefers dark mode" in brain.system
        assert model.requests[-1]["system"][0]["text"] == brain.system


def test_no_memory_path_means_no_memory_instructions(tmp_path):
    from sommus.brain.prompt import system_prompt

    assert "`remember`" not in system_prompt(config(tmp_path))
