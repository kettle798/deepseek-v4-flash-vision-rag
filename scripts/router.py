"""router.py —— 自适应页分类器（pdf-inspector 体检 + 图纸检测）

输入一份 PDF，输出每页的路由标签与可用文本。路由由数据决定，不做预设：

    TEXT    文本层可用且质量好       → 文本直录（快、省）
    TABLE   表格页                  → 文本直录 + 可选 VLM 校验
    GARBLED 文本层是乱码            → VLM 朗读干净渲染图
    SCAN    扫描件/无文本层          → VLM OCR
    GRAPHIC 低文本+高图像（图纸）    → VLM 图纸转录（标注/尺寸/图例）

依赖：
    pdf-inspector（逐页 markdown + needs_ocr + ocr_reason + 表格标记）
    PyMuPDF（图像面积占比，用于图纸检测）

用法：
    python router.py <pdf> [--limit N]     # 打印页标签分布与样例
"""
import argparse
import json
import sys
import warnings
from pathlib import Path

import fitz

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MIN_TEXT_CHARS = 50          # 少于视为"无可用文本"
GRAPHIC_IMAGE_RATIO = 0.25   # 图像面积占比阈值 → 判图纸
GARBLED_STOPWORD_HITS = 8    # 高频词命中 < 此值且字符多 → 判乱码

# 高频英文词（正常英文页必命中多个，字符替换/加密乱码页几乎为 0）
_COMMON_WORDS = set(
    "the a an of and in is to for with on by at as it be this that from or are was were "
    "have has had its their which will not can but also our their you".split())


def _is_garbled(raw_text: str) -> bool:
    """文本层存在但内容为乱码（字符替换/加密）→ True。
    依据：正常英文页必然出现多个高频词；乱码页命中近 0。"""
    if len(raw_text.strip()) < MIN_TEXT_CHARS:
        return False
    import re as _re
    hits = len(set(_re.findall(r"[a-z]+", raw_text.lower())) & _COMMON_WORDS)
    return hits < GARBLED_STOPWORD_HITS


def page_image_ratio(page) -> float:
    """页面图像面积占比（0~1）。PyMuPDF get_image_info 的 bbox 统计。"""
    try:
        infos = page.get_image_info()
    except Exception:
        return 0.0
    if not infos:
        return 0.0
    pw, ph = page.rect.width, page.rect.height
    area = 0.0
    for info in infos:
        bbox = info.get("bbox")
        if bbox:
            area += max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
    return min(1.0, area / max(1e-6, pw * ph))


def classify_pdf(pdf: Path, limit: int = 0):
    """返回 (labels, page_texts, meta)：
    labels:     {页(1-based): 标签}
    page_texts: {页(1-based): 可用文本}
    meta:       pdf-inspector 附加信息（表格页列表/乱码原因等）
    """
    warnings.filterwarnings("ignore")
    import pdf_inspector

    doc = fitz.open(str(pdf))
    total = len(doc)
    n = min(total, limit) if limit > 0 else total

    # pdf-inspector 逐页体检
    try:
        res = pdf_inspector.extract_pages_markdown(str(pdf))
        insp_pages = {pm.page + 1: pm for pm in getattr(res, "pages", [])}
        tbl_pages = {t for t in (getattr(res, "pages_with_tables", None) or [])}
    except Exception as e:
        print(f"[router] pdf-inspector 失败，回退纯 PyMuPDF: {type(e).__name__}: {e}", flush=True)
        insp_pages, tbl_pages = {}, set()

    labels, texts = {}, {}
    for i in range(n):
        p = i + 1
        pm = insp_pages.get(p)
        raw_text = doc[i].get_text()
        md_text = (pm.markdown or "") if pm is not None else raw_text
        n_chars = len(md_text.strip())
        img_ratio = page_image_ratio(doc[i])

        needs_ocr = bool(getattr(pm, "needs_ocr", False)) if pm is not None else (len(raw_text.strip()) < MIN_TEXT_CHARS)
        reason = ""
        if pm is not None:
            reason = str(getattr(pm, "ocr_reason", "") or "").lower()

        # ---- 判定路由 ----
        # 乱码检测优先：文本层有大量字符但读不出正常英文 → GARBLED
        # （pdf-inspector 的 needs_ocr 对"字符多但乱码"的页会漏判，须补刀）
        if needs_ocr and "garbled" in reason:
            label = "GARBLED"
        elif needs_ocr:
            if img_ratio > 0.5:
                label = "GRAPHIC"          # 大图扫描/图纸
            else:
                label = "SCAN"             # 扫描件/无文本
        elif _is_garbled(raw_text):
            label = "GARBLED"              # 有字符但乱码
        elif n_chars < MIN_TEXT_CHARS:
            if img_ratio >= GRAPHIC_IMAGE_RATIO:
                label = "GRAPHIC"          # 图纸页（低文本高图像）
            elif n_chars < 20:
                label = "SCAN"             # 近乎空白/纯图
            else:
                label = "SCAN"
        else:
            label = "TABLE" if p in tbl_pages else "TEXT"

        labels[p] = label
        # 可用文本：乱码页/图纸页/扫描页的原始文本不可用 → 空（待 VLM 转录）
        texts[p] = "" if label in ("GARBLED", "SCAN", "GRAPHIC") else md_text

    doc.close()
    meta = {"total_pages": total, "table_pages": sorted(tbl_pages)}
    return labels, texts, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    labels, texts, meta = classify_pdf(args.pdf, args.limit)
    from collections import Counter
    dist = Counter(labels.values())
    print(f"[router] {args.pdf.name}: {len(labels)} 页")
    print(f"[router] 页型分布: {dict(dist)}")
    print(f"[router] 表格页: {meta['table_pages'][:10]}{'...' if len(meta['table_pages'])>10 else ''}")
    for p, lab in sorted(labels.items()):
        flag = "★" if lab in ("GARBLED", "SCAN", "GRAPHIC") else " "
        print(f"  {flag} p{p:>3} [{lab:<8}] 文本={len(texts[p])} 字符", flush=True)


if __name__ == "__main__":
    main()
