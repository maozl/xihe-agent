# -*- coding: utf-8 -*-
"""Regenerate desktop icon assets from docs/banner.jpg.

Run after adjusting the banner:  python docs/_make_icons.py
Outputs:
  desktop/src/renderer/src/assets/logo.png  (128px, renderer <img>)
  desktop/resources/icon.png                (256px, BrowserWindow icon)
"""
import os

from PIL import Image

SRC = r'E:\xihe-agent\docs\banner.jpg'
OUTS = [
    (r'E:\xihe-agent\desktop\src\renderer\src\assets\logo.png', 128),
    (r'E:\xihe-agent\desktop\resources\icon.png', 256),
]
# Emblem is ~480px centered in the 800x800 banner; 560 leaves a 40px navy margin.
CROP = 560

img = Image.open(SRC).convert('RGB')
w, h = img.size
x0, y0 = (w - CROP) // 2, (h - CROP) // 2
sq = img.crop((x0, y0, x0 + CROP, y0 + CROP))
for path, size in OUTS:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sq.resize((size, size), Image.LANCZOS).save(path)
    print('wrote %s (%dpx)' % (path, size))
