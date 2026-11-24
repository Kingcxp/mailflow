"""Public identity and compatibility with the project's original name."""

PRODUCT_NAME = "Waymark Mail"
COMMAND_NAMES = frozenset({"waymark", "mailflow"})


def canonical_repository_url(value: str) -> str:
    """Read old official repository addresses using their renamed locations."""
    for host in ("https://github.com/", "https://raw.githubusercontent.com/"):
        for old, new in (
            ("mailflow-repo", "waymark-mail-repo"),
            ("waymark-mail-plugins", "waymark-mail-repo"),
            ("mailflow", "waymark-mail"),
        ):
            prefix = host + "Kingcxp/" + old
            plain = value.removeprefix("git+")
            if plain == prefix or plain.startswith((prefix + "/", prefix + ".git", prefix + "@")):
                return value.replace(prefix, host + "Kingcxp/" + new, 1)
    return value
