"""Repo memory: what GhostPatch knows about a project before it starts.

Two sources, both plain Markdown:
- `GHOSTPATCH.md` at the repository root: written by the team, committed, shared.
- `.ghostpatch/memory.md`: facts the agent learned itself with the `remember` tool, such as
  how to run the tests or a convention it discovered. Kept local and short.
"""

from __future__ import annotations

import time
from pathlib import Path

TEAM_FILE = "GHOSTPATCH.md"
LEARNED_FILE = Path(".ghostpatch") / "memory.md"
MAX_NOTES = 40
MAX_NOTE_CHARS = 300
MAX_PROMPT_CHARS = 5000


def learned_path(repo: Path) -> Path:
    return repo / LEARNED_FILE


def learned_notes(repo: Path) -> list[str]:
    path = learned_path(repo)
    if not path.is_file():
        return []
    return [line[2:].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]


def remember(repo: Path, note: str) -> str:
    """Add a note (deduplicated, oldest dropped past MAX_NOTES). Returns what was stored."""
    note = " ".join(note.split())[:MAX_NOTE_CHARS]
    if not note:
        raise ValueError("The note is empty.")
    notes = [n for n in learned_notes(repo) if n.lower() != note.lower()]
    notes.append(note)
    notes = notes[-MAX_NOTES:]
    path = learned_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    header = f"# What GhostPatch learned about this project\n<!-- updated {time.strftime('%Y-%m-%d %H:%M')} -->\n\n"
    path.write_text(header + "\n".join(f"- {n}" for n in notes) + "\n", encoding="utf-8")
    return note


def forget_all(repo: Path) -> int:
    notes = learned_notes(repo)
    path = learned_path(repo)
    if path.is_file():
        path.unlink()
    return len(notes)


def prompt_section(repo: Path) -> str:
    """The memory as text for the agent's first message ('' when there is none)."""
    parts = []
    team = repo / TEAM_FILE
    if team.is_file():
        parts.append(f"Team notes from {TEAM_FILE}:\n" + team.read_text(encoding="utf-8").strip())
    notes = learned_notes(repo)
    if notes:
        parts.append("What you learned in earlier runs on this project:\n" + "\n".join(f"- {n}" for n in notes))
    text = "\n\n".join(parts)
    return text[:MAX_PROMPT_CHARS]
