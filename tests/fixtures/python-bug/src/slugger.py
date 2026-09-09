"""Tiny intentionally broken repository used for deterministic agent tests."""


def slugify(title: str) -> str:
    """Return a URL slug (the repeated-space behavior is intentionally broken)."""
    return title.lower().replace(" ", "-")
