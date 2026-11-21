"""Host-independent color schemes used by built-ins and theme plugins.

Catppuccin colors: https://catppuccin.com/palette/ (MIT).
Other built-ins follow the published Dracula, Nord, Gruvbox, Solarized, Tokyo
Night, and Microsoft VS Code Modern palettes.
Plugins return a ColorScheme from a zero-argument factory registered with
``registrar.add_theme`` or ``@PLUGIN.theme``. Overrides inherit a full palette
so a small theme plugin can still style every MailFlow surface consistently.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

DEFAULT_THEME = "catppuccin-macchiato"
TERMINAL_THEME = "terminal-auto"
Flavor = Literal["latte", "frappe", "macchiato", "mocha"]

_COLOR_NAMES = (
    "rosewater flamingo pink mauve red maroon peach yellow green teal sky sapphire "
    "blue lavender text subtext1 subtext0 overlay2 overlay1 overlay0 "
    "surface2 surface1 surface0 base mantle crust"
).split()

_PALETTES: dict[Flavor, str] = {
    "latte": (
        "dc8a78 dd7878 ea76cb 8839ef d20f39 e64553 fe640b df8e1d 40a02b 179299 "
        "04a5e5 209fb5 1e66f5 7287fd 4c4f69 5c5f77 6c6f85 7c7f93 8c8fa1 9ca0b0 "
        "acb0be bcc0cc ccd0da eff1f5 e6e9ef dce0e8"
    ),
    "frappe": (
        "f2d5cf eebebe f4b8e4 ca9ee6 e78284 ea999c ef9f76 e5c890 a6d189 81c8be "
        "99d1db 85c1dc 8caaee babbf1 c6d0f5 b5bfe2 a5adce 949cbb 838ba7 737994 "
        "626880 51576d 414559 303446 292c3c 232634"
    ),
    "macchiato": (
        "f4dbd6 f0c6c6 f5bde6 c6a0f6 ed8796 ee99a0 f5a97f eed49f a6da95 8bd5ca "
        "91d7e3 7dc4e4 8aadf4 b7bdf8 cad3f5 b8c0e0 a5adcb 939ab7 8087a2 6e738d "
        "5b6078 494d64 363a4f 24273a 1e2030 181926"
    ),
    "mocha": (
        "f5e0dc f2cdcd f5c2e7 cba6f7 f38ba8 eba0ac fab387 f9e2af a6e3a1 94e2d5 "
        "89dceb 74c7ec 89b4fa b4befe cdd6f4 bac2de a6adc8 9399b2 7f849c 6c7086 "
        "585b70 45475a 313244 1e1e2e 181825 11111b"
    ),
}

# Sources: github.com/dracula/visual-studio-code, nordtheme/nord, morhetz/gruvbox,
# altercation/solarized, folke/tokyonight.nvim, and microsoft/vscode.
# Each entry groups the 14 accents, 6 text tones, and 6 surfaces in _COLOR_NAMES order.
_EXTRA_THEMES: dict[
    str,
    tuple[str, bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]],
] = {
    "dracula": (
        "Dracula",
        True,
        (
            "#f8f8f2", "#ff79c6", "#ff79c6", "#bd93f9", "#ff5555", "#ff5555", "#ffb86c",
            "#f1fa8c", "#50fa7b", "#8be9fd", "#8be9fd", "#8be9fd", "#bd93f9", "#bd93f9",
        ),
        ("#f8f8f2", "#f8f8f2", "#6272a4", "#6272a4", "#44475a", "#44475a"),
        ("#6272a4", "#44475a", "#44475a", "#282a36", "#282a36", "#282a36"),
    ),
    "nord": (
        "Nord",
        True,
        (
            "#d8dee9", "#bf616a", "#b48ead", "#b48ead", "#bf616a", "#bf616a", "#d08770",
            "#ebcb8b", "#a3be8c", "#8fbcbb", "#88c0d0", "#81a1c1", "#5e81ac", "#b48ead",
        ),
        ("#eceff4", "#e5e9f0", "#d8dee9", "#4c566a", "#434c5e", "#3b4252"),
        ("#4c566a", "#434c5e", "#3b4252", "#2e3440", "#3b4252", "#2e3440"),
    ),
    "gruvbox-dark": (
        "Gruvbox Dark",
        True,
        (
            "#fbf1c7", "#d3869b", "#d3869b", "#d3869b", "#fb4934", "#cc241d", "#fe8019",
            "#fabd2f", "#b8bb26", "#8ec07c", "#83a598", "#458588", "#83a598", "#d3869b",
        ),
        ("#fbf1c7", "#ebdbb2", "#d5c4a1", "#928374", "#7c6f64", "#665c54"),
        ("#665c54", "#504945", "#3c3836", "#282828", "#32302f", "#1d2021"),
    ),
    "gruvbox-light": (
        "Gruvbox Light",
        False,
        (
            "#282828", "#b16286", "#b16286", "#b16286", "#9d0006", "#9d0006", "#af3a03",
            "#b57614", "#79740e", "#427b58", "#076678", "#076678", "#076678", "#b16286",
        ),
        ("#282828", "#3c3836", "#504945", "#665c54", "#7c6f64", "#928374"),
        ("#a89984", "#bdae93", "#d5c4a1", "#fbf1c7", "#f2e5bc", "#f9f5d7"),
    ),
    "solarized-dark": (
        "Solarized Dark",
        True,
        (
            "#93a1a1", "#d33682", "#d33682", "#6c71c4", "#dc322f", "#dc322f", "#cb4b16",
            "#b58900", "#859900", "#2aa198", "#2aa198", "#268bd2", "#268bd2", "#6c71c4",
        ),
        ("#839496", "#93a1a1", "#657b83", "#586e75", "#657b83", "#586e75"),
        ("#586e75", "#073642", "#073642", "#002b36", "#073642", "#002b36"),
    ),
    "solarized-light": (
        "Solarized Light",
        False,
        (
            "#586e75", "#d33682", "#d33682", "#6c71c4", "#dc322f", "#dc322f", "#cb4b16",
            "#b58900", "#859900", "#2aa198", "#2aa198", "#268bd2", "#268bd2", "#6c71c4",
        ),
        ("#657b83", "#586e75", "#839496", "#93a1a1", "#839496", "#93a1a1"),
        ("#93a1a1", "#eee8d5", "#eee8d5", "#fdf6e3", "#eee8d5", "#fdf6e3"),
    ),
    "tokyo-night": (
        "Tokyo Night",
        True,
        (
            "#c0caf5", "#f7768e", "#ff007c", "#bb9af7", "#f7768e", "#db4b4b", "#ff9e64",
            "#e0af68", "#9ece6a", "#73daca", "#7dcfff", "#2ac3de", "#7aa2f7", "#9d7cd8",
        ),
        ("#c0caf5", "#a9b1d6", "#737aa2", "#565f89", "#414868", "#3b4261"),
        ("#545c7e", "#414868", "#292e42", "#1a1b26", "#16161e", "#0c0e14"),
    ),
    "vscode-dark-modern": (
        "VS Code Dark Modern",
        True,
        (
            "#d7ba7d", "#c586c0", "#c586c0", "#c586c0", "#f85149", "#d16969", "#ce9178",
            "#dcdcaa", "#4ec9b0", "#4ec9b0", "#4fc1ff", "#569cd6", "#0078d4", "#c586c0",
        ),
        ("#cccccc", "#9d9d9d", "#868686", "#6e7681", "#616161", "#3c3c3c"),
        ("#3c3c3c", "#313131", "#2b2b2b", "#1f1f1f", "#181818", "#181818"),
    ),
    "vscode-light-modern": (
        "VS Code Light Modern",
        False,
        (
            "#795e26", "#af00db", "#af00db", "#af00db", "#ee0000", "#811f3f", "#d16969",
            "#795e26", "#267f99", "#267f99", "#0070c1", "#0451a5", "#0000ff", "#af00db",
        ),
        ("#3b3b3b", "#616161", "#767676", "#8a8a8a", "#a6a6a6", "#c8c8c8"),
        ("#cecece", "#e5e5e5", "#f3f3f3", "#ffffff", "#f8f8f8", "#f3f3f3"),
    ),
}


class ColorScheme(BaseModel):
    """Data-only theme contract; the core never imports a UI framework."""

    name: str
    base: Flavor = "macchiato"
    dark: bool | None = None
    colors: dict[str, str] = Field(default_factory=dict)

    @field_validator("colors")
    @classmethod
    def valid_colors(cls, colors: dict[str, str]) -> dict[str, str]:
        for name, color in colors.items():
            if name not in _COLOR_NAMES or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError(f"invalid theme color {name!r}: expected a palette key and #RRGGBB")
        return colors

    def palette(self) -> dict[str, str]:
        return {
            **dict(zip(_COLOR_NAMES, ("#" + c for c in _PALETTES[self.base].split()), strict=True)),
            **self.colors,
        }

    @property
    def is_dark(self) -> bool:
        return self.dark if self.dark is not None else self.base != "latte"


def builtin_themes() -> dict[str, ColorScheme]:
    themes = {
        "catppuccin-" + flavor: ColorScheme(
            name="Catppuccin " + ("Frappé" if flavor == "frappe" else flavor.title()), base=flavor
        )
        for flavor in _PALETTES
    }
    for theme_id, (name, dark, accents, tones, surfaces) in _EXTRA_THEMES.items():
        colors = dict(zip(_COLOR_NAMES, (*accents, *tones, *surfaces), strict=True))
        themes[theme_id] = ColorScheme(
            name=name, base="macchiato", dark=dark, colors=colors
        )
    themes[TERMINAL_THEME] = ColorScheme(name="Match terminal", base="macchiato", dark=True)
    return themes
