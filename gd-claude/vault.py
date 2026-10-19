"""GD Claude's long-term memory, stored as notes in an Obsidian vault.

Everything lives in one folder ("GD Claude/") inside the vault, as plain
Markdown, so the player can read and edit what Claude has learned. Claude
reads the notes at the start of each session and writes to them as it learns.
"""

import re
import time
from pathlib import Path

FOLDER = "GD Claude"
MAX_CONTEXT_CHARS = 60_000  # how much of the vault gets loaded into each session

STARTER_NOTES = {
    "My Style": (
        "---\ntags: [gd-claude]\n---\n# My Style\n\n"
        "What GD Claude knows about how I like my levels. Edit this freely.\n\n"
        "- (nothing yet -- tell GD Claude what you like!)\n"
    ),
    "Design Rules": (
        "---\ntags: [gd-claude]\n---\n# Design Rules\n\n"
        "Lessons GD Claude has learned about making good levels "
        "(from playtests, feedback and studying online levels).\n\n"
    ),
    "Feedback Log": (
        "---\ntags: [gd-claude]\n---\n# Feedback Log\n\n"
        "Every time I reject a design and say why, it lands here.\n\n"
    ),
}


def _safe_title(title):
    title = re.sub(r'[\\/:*?"<>|#^\[\]]', "", str(title)).strip().strip(".")
    if not title:
        raise ValueError("note title is empty")
    return title[:100]


class Vault:
    def __init__(self, vault_path):
        root = Path(vault_path).expanduser()
        if not root.is_dir():
            raise FileNotFoundError(f"Obsidian vault folder not found: {root}")
        self.dir = root / FOLDER
        (self.dir / "Levels").mkdir(parents=True, exist_ok=True)
        for title, body in STARTER_NOTES.items():
            path = self.dir / f"{title}.md"
            if not path.exists():
                path.write_text(body, encoding="utf-8")

    def _path(self, title):
        parts = [_safe_title(p) for p in str(title).split("/") if p.strip()]
        if not parts or len(parts) > 2 or (len(parts) == 2 and parts[0] != "Levels"):
            raise ValueError("notes go at the top level or in 'Levels/<level name>'")
        return self.dir.joinpath(*parts).with_suffix(".md")

    def titles(self):
        return sorted(str(p.relative_to(self.dir).with_suffix("")).replace("\\", "/")
                      for p in self.dir.rglob("*.md"))

    def read(self, title):
        path = self._path(title)
        if not path.exists():
            raise KeyError(f"No note called '{title}'. Notes: {', '.join(self.titles())}")
        return path.read_text(encoding="utf-8")

    def write(self, title, content, mode="append"):
        path = self._path(title)
        if mode == "replace" or not path.exists():
            if not content.lstrip().startswith("---"):
                name = path.stem
                content = f"---\ntags: [gd-claude]\n---\n# {name}\n\n{content.strip()}\n"
            path.write_text(content, encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n{content.strip()}\n")
        return str(path.relative_to(self.dir).with_suffix("")).replace("\\", "/")

    def log_feedback(self, level, feedback):
        stamp = time.strftime("%Y-%m-%d %H:%M")
        self.write("Feedback Log", f"- {stamp} on [[{level}]]: {feedback}")

    def context(self):
        """All notes as one block of text for the system prompt (main notes first)."""
        order = [t for t in STARTER_NOTES if t in self.titles()]
        order += [t for t in self.titles() if t not in order]
        chunks, used = [], 0
        for title in order:
            text = self.read(title)
            if used + len(text) > MAX_CONTEXT_CHARS:
                chunks.append(f"(more notes not loaded -- use read_note: {', '.join(order[len(chunks):])})")
                break
            chunks.append(f'<note title="{title}">\n{text.strip()}\n</note>')
            used += len(text)
        return "\n\n".join(chunks)
