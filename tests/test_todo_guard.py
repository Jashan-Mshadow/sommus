"""The to-do note's `updated:` date, so a stale list is announced as stale."""

from datetime import date

from sommus.brain import campus


def test_the_updated_date_is_read_from_the_front_matter(tmp_path):
    note = tmp_path / "TODO.md"
    note.write_text("---\ntype: todo\nupdated: 2026-11-16\n---\n# TODO\n")
    assert campus.todo_updated(note) == date(2026, 11, 16)
    note.write_text("# TODO\nno front matter\n")
    assert campus.todo_updated(note) is None
    assert campus.todo_updated(tmp_path / "missing.md") is None
