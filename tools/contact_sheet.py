"""Contact sheet of QA turntable strips. Needs Pillow (run with the ComfyUI venv python).

    python -I tools/contact_sheet.py out/srd-5.2.1/_contact_pilot.png out/srd-5.2.1/<id> ...
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TILE_W, TILE_H, LABEL_H, COLS = 1024, 256, 40, 2


def font(size):
    for p in ("/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
              "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
              "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def main():
    dst, dirs = Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]
    rows = (len(dirs) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * TILE_W, rows * (TILE_H + LABEL_H)), "white")
    draw, f = ImageDraw.Draw(sheet), font(22)
    for i, d in enumerate(dirs):
        x, y = (i % COLS) * TILE_W, (i // COLS) * (TILE_H + LABEL_H)
        man = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {}
        thumb = d / "thumb.png"
        if thumb.exists():
            sheet.paste(Image.open(thumb).convert("RGB").resize((TILE_W, TILE_H)), (x, y + LABEL_H))
        else:
            draw.text((x + 20, y + LABEL_H + 100), "MISSING (see manifest errors)", fill="red", font=f)
        label = (f"{man.get('name', d.name)}  |  {man.get('body_plan', '?')}, {man.get('size', '?')}  |  "
                 f"{man.get('tri_count', '?')} tris  |  {man.get('scale_m', '?')} m")
        draw.text((x + 10, y + 8), label, fill="black", font=f)
        draw.rectangle([x, y, x + TILE_W - 1, y + TILE_H + LABEL_H - 1], outline=(180, 180, 180))
    sheet.save(dst)
    print(dst)


if __name__ == "__main__":
    main()
