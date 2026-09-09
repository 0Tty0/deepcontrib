"""Application-owned skill texts for deterministic agent stages."""

from pathlib import Path


def load_skill(name: str) -> str:
    """Load an application skill; repository content never overrides this path."""
    if not name or Path(name).name != name or not name.endswith(".md"):
        raise ValueError("skill name must be a Markdown filename")
    path = Path(__file__).with_name("skills") / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("skill is not available") from exc
