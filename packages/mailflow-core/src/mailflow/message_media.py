"""Optional image transport and local summary cards; no remote rendering service."""

from __future__ import annotations

from abc import ABC, abstractmethod
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from mailflow.domain import NotificationCapabilities, NotificationImage, NotificationMessage


class ImageNotifier(ABC):
    capabilities = NotificationCapabilities(images=True)

    @abstractmethod
    async def push_to_target(self, target: str, text: str) -> None: ...

    @abstractmethod
    async def push_image_to_target(self, target: str, image: NotificationImage) -> None: ...

    async def send_message(self, target: str, message: NotificationMessage) -> None:
        if message.text:
            await self.push_to_target(target, message.text)
        for image in message.images:
            await self.push_image_to_target(target, image)


def summary_card(text: str, language: str) -> NotificationImage | None:
    fonts = (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    )
    font_path = next((p for p in fonts if Path(p).is_file()), None)
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    if font_path:
        font = ImageFont.truetype(font_path, 24)
    elif language.startswith("zh"):
        return None
    else:
        font = ImageFont.load_default(size=24)
    canvas = Image.new("RGB", (720, 1600), "#f6f8fc")
    draw = ImageDraw.Draw(canvas)
    y = 28
    for paragraph in text[:1800].splitlines():
        line = ""
        for character in paragraph:
            if line and draw.textlength(line + character, font=font) > 660:
                draw.text((28, y), line, font=font, fill="#1f2937")
                y += 36
                line = ""
                if y > 1500:
                    break
            line += character
        draw.text((28, y), line, font=font, fill="#1f2937")
        y += 40
        if y > 1500:
            break
    output = BytesIO()
    canvas.crop((0, 0, 720, min(1600, y + 20))).save(output, format="PNG")
    return NotificationImage(filename="summary.png", data=output.getvalue())
