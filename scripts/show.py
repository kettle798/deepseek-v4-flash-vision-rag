"""show.py —— 把 PDF 指定页渲染为高清图片，供展示给用户（无需先建索引）。

用法：
  python show.py <pdf> --pages 6,7 [--dpi 220] [--out pdf-vision-out]
"""
import argparse
import sys
from pathlib import Path

import fitz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAX_SIDE_PX = 4096


def main():
    ap = argparse.ArgumentParser(description="Render PDF pages to images")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--pages", required=True, help="页码，逗号分隔，如 6,7 或 6-9")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--out", type=Path, default=Path("pdf-vision-out"))
    args = ap.parse_args()

    pdf = args.pdf.resolve()
    if not pdf.is_file():
        sys.exit(f"PDF not found: {pdf}")

    pages = set()
    for part in args.pages.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part:
            pages.add(int(part))

    doc = fitz.open(str(pdf))
    stem = pdf.name.rsplit(".", 1)[0]
    args.out.mkdir(exist_ok=True)
    for p in sorted(pages):
        if not 1 <= p <= len(doc):
            print(f"[skip] page {p} out of range (1-{len(doc)})")
            continue
        page = doc[p - 1]
        long_side_in = max(page.rect.width, page.rect.height) / 72
        dpi = max(72, min(args.dpi, int(MAX_SIDE_PX / long_side_in)))
        dst = args.out / f"{stem}_p{p}.png"
        page.get_pixmap(dpi=dpi).save(str(dst))
        print(dst.resolve())


if __name__ == "__main__":
    main()
