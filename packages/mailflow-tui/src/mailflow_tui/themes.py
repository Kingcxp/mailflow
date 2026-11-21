"""One palette for Textual widgets, Rich content and terminal colors."""

from __future__ import annotations

from typing import Any

from mailflow.themes import ColorScheme, builtin_themes
from rich.style import Style
from rich.terminal_theme import TerminalTheme
from rich.theme import Theme as RichTheme
from textual.app import App
from textual.color import Color
from textual.theme import Theme

_CONTENT_STYLES = {
    "title": ("text", "bold"),
    "heading": ("subtext1", "bold"),
    "text": ("text", ""),
    "muted": ("subtext0", ""),
    "time": ("peach", ""),
    "tag": ("mauve", ""),
    "link": ("blue", "underline"),
    "overdue0": ("subtext0", "not bold"),
    "overdue1": ("overlay2", "not bold"),
    "overdue2": ("overlay0", "not bold"),
}


def _content_styles(palette: dict[str, str]) -> dict[str, str]:
    return {
        "mf-" + name: f"{attributes} {palette[color]}".strip()
        for name, (color, attributes) in _CONTENT_STYLES.items()
    }


def theme_catalog(service: Any) -> dict[str, ColorScheme]:
    schemes = builtin_themes()
    provider = getattr(service, "color_schemes", None)
    if callable(provider):
        schemes.update(provider())
    return schemes


def textual_theme(name: str, scheme: ColorScheme) -> Theme:
    p = scheme.palette()
    selection = Color.parse(p["base"]).blend(Color.parse(p["blue"]), 0.25).hex
    return Theme(
        name=name,
        primary=p["blue"],
        secondary=p["mauve"],
        accent=p["lavender"],
        foreground=p["text"],
        background=p["base"],
        surface=p["base"],
        panel=p["mantle"],
        success=p["green"],
        warning=p["yellow"],
        error=p["red"],
        dark=scheme.is_dark,
        variables={
            **{"ctp-" + key: value for key, value in p.items()},
            **_content_styles(p),
            "text": p["text"],
            "text-muted": p["subtext0"],
            "text-disabled": p["overlay0"],
            "border": p["surface2"],
            "border-blurred": p["surface1"],
            "block-cursor-background": selection,
            "block-cursor-foreground": p["text"],
            "block-cursor-text-style": "none",
            "block-cursor-blurred-background": selection,
            "block-cursor-blurred-foreground": p["text"],
            "block-cursor-blurred-text-style": "none",
            "block-hover-background": p["surface0"],
            "input-cursor-background": p["rosewater"],
            "input-cursor-foreground": p["base"],
            "input-selection-background": selection,
            "input-selection-foreground": p["text"],
            "screen-selection-background": selection,
            "screen-selection-foreground": p["text"],
            "input-background": p["mantle"],
            "input-background-focus": p["mantle"],
            "input-border-blurred": p["surface1"],
            "input-border-focus": p["lavender"],
            "input-placeholder-foreground": p["subtext0"],
            "footer-background": p["mantle"],
            "footer-key-foreground": p["blue"],
            "footer-description-foreground": p["subtext1"],
            "button-focus-text-style": "bold underline",
            "button-on-accent": p["base"],
            "button-on-warning": p["base"] if scheme.is_dark else p["text"],
            "scrollbar": p["surface2"],
            "scrollbar-hover": p["overlay0"],
            "scrollbar-active": p["lavender"],
            "scrollbar-background": p["mantle"],
            "scrollbar-background-hover": p["mantle"],
            "scrollbar-background-active": p["mantle"],
            "link-color": p["blue"],
            "link-color-hover": p["sapphire"],
            "markdown-h1-color": p["text"],
            "markdown-h2-color": p["text"],
            "markdown-h3-color": p["text"],
            "markdown-h4-color": p["subtext1"],
            "markdown-h5-color": p["subtext1"],
            "markdown-h6-color": p["subtext1"],
            "surface-lighten-1": p["surface0"],
            "surface-lighten-2": p["surface1"],
            "surface-lighten-3": p["surface2"],
            "surface-darken-1": p["mantle"],
            "surface-darken-2": p["crust"],
            "surface-darken-3": p["crust"],
        },
    )


def apply_color_scheme(
    app: App[Any], name: str, scheme: ColorScheme, *, replace_rich: bool = False
) -> None:
    p = scheme.palette()

    def rgb(key: str) -> tuple[int, int, int]:
        color = p[key].lstrip("#")
        return int(color[:2], 16), int(color[2:4], 16), int(color[4:], 16)

    terminal = TerminalTheme(
        rgb("base"),
        rgb("text"),
        [
            rgb(key)
            for key in ("surface1", "red", "green", "yellow", "blue", "mauve", "teal", "subtext0")
        ],
        [rgb(key) for key in ("overlay1", "red", "green", "yellow", "blue", "pink", "sky", "text")],
    )
    app.ansi_theme_dark = app.ansi_theme_light = terminal
    # The same tokens work in Textual markup and Rich renderables. Rich resolves
    # spans through its console; Textual resolves the matching CSS variables.
    styles = {"$" + key: Style.parse(value) for key, value in _content_styles(p).items()}
    for key, color in p.items():
        for attributes in ("", "bold", "dim", "underline", "dim bold", "bold dim"):
            style_name = f"{attributes} $ctp-{key}".strip()
            styles[style_name] = Style.parse(f"{attributes} {color}".strip())
    styles.update(
        {
            "markdown.h1": Style(color=p["text"], bold=True),
            "markdown.h2": Style(color=p["text"], bold=True),
            "markdown.h3": Style(color=p["text"], bold=True),
            "markdown.code": Style(color=p["peach"], bgcolor=p["surface0"]),
            "markdown.link": Style(color=p["blue"], underline=True),
            "markdown.link_url": Style(color=p["blue"], underline=True),
            "markdown.block_quote": Style(color=p["subtext0"]),
            "markdown.hr": Style(color=p["surface2"]),
        }
    )
    rich_theme = RichTheme(styles)
    ui_theme = textual_theme(name, scheme)
    app.register_theme(ui_theme)
    if replace_rich:
        app.console.pop_theme()
    app.console.push_theme(rich_theme)
    unchanged_name = app.theme == name
    app.theme = name
    if unchanged_name and app.is_running:
        app.refresh_css(animate=False)


def urgency_style(urgency: Any) -> str:
    """Keep stored priority semantics while using the active theme's colors."""
    return {
        "urgent": "$ctp-red",
        "important": "$ctp-yellow",
        "info": "$ctp-green",
    }.get(str(urgency), "$ctp-subtext0")
