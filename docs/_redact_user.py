# -*- coding: utf-8 -*-
"""Replace the personal username in tracked screenshots with 'user'.

PaddleOCR (3.x: rec_texts + rec_boxes) locates each text line; monospace
layout gives the substring's x-range from its index in the line text. The
region is repainted in the line's background color and 'user' is drawn in
Consolas at the line's text color. A verification OCR pass must find no
occurrence afterwards.
"""
import logging

logging.disable(logging.WARNING)

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from paddleocr import PaddleOCR

OLD = "zzmao"
NEW = "user"
FONT = r"C:\Windows\Fonts\consola.ttf"

IMAGES = [
    r"E:\xihe-agent\docs\images\cli-chat-tools.png",
    r"E:\xihe-agent\docs\images\desktop-steer.png",
]


def ocr_lines(ocr, path):
    """[(text, (x0, y0, x1, y1))] for one image (paddle 3.x result shape)."""
    res = ocr.ocr(path)[0]
    boxes = res["rec_boxes"]
    out = []
    for text, rec in zip(list(res["rec_texts"]), list(boxes)):
        x0, y0, x1, y1 = (int(v) for v in rec)
        out.append((text, (x0, y0, x1, y1)))
    return out


def line_colors(img, rect):
    """(bg, fg) for a text line: bg = brightest frequent color, fg =
    darkest frequent color among the crop's dominant colors."""
    crop = img.crop(rect)
    a = np.asarray(crop.convert("RGB"))
    colors, counts = np.unique(a.reshape(-1, 3), axis=0, return_counts=True)
    order = np.argsort(-counts)
    top = colors[order[:6]]
    bg = top[np.argmax(top.sum(axis=1))]
    fg = top[np.argmin(top.sum(axis=1))]
    return tuple(int(c) for c in bg), tuple(int(c) for c in fg)


def units(s):
    """Monospace advance units: CJK full-width = 2, everything else = 1."""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def main():
    ocr = PaddleOCR(lang="ch", enable_mkldnn=False)
    for path in IMAGES:
        img = Image.open(path).convert("RGB")
        hits = 0
        for text, (x0, y0, x1, y1) in ocr_lines(ocr, path):
            idx = text.lower().find(OLD)
            if idx < 0:
                continue
            hits += 1
            total = units(text)
            before = units(text[:idx])
            uw = (x1 - x0) / total
            rx = x0 + before * uw
            rw = units(OLD) * uw
            bg, fg = line_colors(img, (x0, y0, x1, y1))
            draw = ImageDraw.Draw(img)
            draw.rectangle([rx - 1, y0 - 1, rx + rw + 1, y1 + 1], fill=bg)
            # Consolas advance ~= 0.55em, so size ~= uw / 0.55; also fit the
            # line height.
            size = max(10, int(min((y1 - y0) * 0.85, uw * 1.8)))
            font = ImageFont.truetype(FONT, size)
            draw.text((rx, y0 + (y1 - y0 - size) / 2), NEW, fill=fg, font=font)
            print("%s: replaced at x~%d y~%d (line: %s)" % (
                path.split("\\")[-1], rx, (y0 + y1) / 2, text[:60]))
        if hits:
            img.save(path)
        leaks = [t for t, _r in ocr_lines(ocr, path) if OLD in t.lower()]
        print("%s: %d replaced, verify-leaks=%d" % (path.split("\\")[-1], hits, len(leaks)))
        for t in leaks:
            print("  LEAK:", t[:80])


if __name__ == "__main__":
    main()
