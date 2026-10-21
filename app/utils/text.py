import html


def esc(value: object) -> str:
    """Escape untrusted text for parse_mode=HTML (the bot default)."""
    return html.escape("" if value is None else str(value), quote=False)


def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(limit - 1, 0)] + "…"
