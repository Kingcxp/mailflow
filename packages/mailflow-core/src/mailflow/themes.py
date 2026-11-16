"""Host-independent color schemes used by built-ins and theme plugins.

Catppuccin colors: https://catppuccin.com/palette/ (MIT).
Plugins return a ColorScheme from a zero-argument factory registered with
``registrar.add_theme`` or ``@PLUGIN.theme``. Overrides inherit a full palette
so a small theme plugin can still style every MailFlow surface consistently.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

DEFAULT_THEME = "catppuccin-macchiato"
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
    return {
        "catppuccin-" + flavor: ColorScheme(
            name="Catppuccin " + ("Frappé" if flavor == "frappe" else flavor.title()), base=flavor
        )
        for flavor in _PALETTES
    }
