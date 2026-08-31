"""
Agentic RAG 工具集：5 个原子能力，由 AI 主循环自主调用。

核心设计原则：本文件只提供"能力"，不包含任何"流程"。
- 搜什么词、看哪页、要不要看图、给 VLM 什么指令、何时放弃
  ——全部由调用者（模型，ReAct 循环里的 AI）实时决定。
- 没有任何预定义的检索词词典、页分类阈值流水线、固定三步流程。

用法（由 AI 在循环中按需调用）：
    import agentic_tools as T
    info = T.inspect_pdf(pdf)                    # 阶段0 侦察
    hits = T.search_pages(pdf, "appointed director")   # 阶段2 搜索（可换词反复调）
    txt  = T.read_text(pdf, 67)                   # 读文本层
    txt  = T.read_vision(pdf, 67, "逐字朗读整页内容")  # 看图（指令由 AI 构造）
    ok, why = T.verify_quote("...", quote, [(67, md)], "number", 112)  # 出答案前自检
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# 索引定位（人式流程第 1 步的"目录"）：.cache/<sha256前16>/index.json
# ingest.py 预建的索引 = 每页转录文本(page_texts) + 结构目录(pages: type/
# headings/keywords/summary) + 书签(outline)。scan_index 优先查它，像人先翻目录。
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=64)
def _find_index(pdf_path):
    """定位 PDF 的预建索引（.cache/<sha256前16>/index.json）；无则返回 None。"""
    try:
        h = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None
    idx = HERE.parent / ".cache" / h / "index.json"
    if not idx.exists():
        return None
    try:
        j = json.loads(idx.read_text(encoding="utf-8"))
    except Exception:
        return None
    # 校验：索引内 pdf 文件名与传入一致，sha256 前缀匹配（防不同文件同前缀）
    name = Path(str(j.get("pdf", ""))).name
    if name and name != Path(pdf_path).name:
        return None
    if j.get("sha256") and not str(j["sha256"]).startswith(h):
        return None
    return j


@functools.lru_cache(maxsize=32)
def _page_texts_live(pdf_path):
    """会话级文本层缓存：一次提取全 PDF 每页文本（降级路径用，换词不重复全量解析）。"""
    import fitz
    doc = fitz.open(str(pdf_path))
    texts = tuple(doc[i].get_text() for i in range(len(doc)))
    doc.close()
    return texts


def _scan_via_index(idx, keywords, max_per_keyword):
    """在预建索引里查目录：页码 + 该页标题/类型 + 命中上下文（不打开 PDF）。"""
    ptexts = idx.get("page_texts") or []
    meta = {p.get("page"): p for p in (idx.get("pages") or []) if isinstance(p, dict)}
    out = []
    for kw in [k for k in keywords if k]:
        kl = kw.lower()
        for i, t in enumerate(ptexts):
            low = t.lower()
            for m in list(re.finditer(re.escape(kl), low))[:max_per_keyword]:
                s = max(0, m.start() - 120)
                e = min(len(t), m.end() + 120)
                md = meta.get(i + 1, {})
                out.append({
                    "keyword": kw,
                    "page": i + 1,               # 1基
                    "page0": i,                  # 0基
                    "excerpt": re.sub(r"\s+", " ", t[s:e]).strip(),
                    "title": (md.get("headings") or [None])[0],
                    "type": md.get("type"),
                    "source": "index",
                })
    out.sort(key=lambda x: x["page"])
    return out


def _scan_live(pdf_path, keywords, context, max_per_keyword, pages):
    """降级：无预建索引时即时扫描文本层（会话内只全量解析一次）。"""
    texts = _page_texts_live(pdf_path)
    total = len(texts)
    page_range = pages if pages is not None else range(total)
    out = []
    for i in page_range:
        if not (0 <= i < total):
            continue
        txt, low = texts[i], texts[i].lower()
        for kw in [k for k in keywords if k]:
            kl = kw.lower()
            for m in list(re.finditer(re.escape(kl), low))[:max_per_keyword]:
                s = max(0, m.start() - context)
                e = min(len(txt), m.end() + context)
                out.append({
                    "keyword": kw,
                    "page": i + 1,
                    "page0": i,
                    "excerpt": re.sub(r"\s+", " ", txt[s:e]).strip(),
                    "title": None,
                    "type": None,
                    "source": "live",
                })
    out.sort(key=lambda x: x["page"])
    return out

# ---------------------------------------------------------------------------
# 工具 1：侦察 —— 摸清 PDF 是什么（AI 判断后续策略的依据）
# ---------------------------------------------------------------------------
def inspect_pdf(pdf_path, n_pages: int = 0, dpi: int = 150) -> dict:
    """侦察 PDF 的基本盘：页数、大纲、每页文本量/图像量、可疑页标记。

    返回：
        {
          "pages": 总页数,
          "outline": [(level, title, page), ...] 前 30 条（若有），
          "page_stats": [{page(0基), chars, images}, ...] 抽样或全量,
          "flags": 低文本+高图像页（疑似图纸/扫描）,
          "low_text_pages": 文本极少页（<50字符）,
        }
    由 AI 据此判断：这是文本型还是图文型？哪些区域值得重点看？
    """
    import fitz
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    limit = total if (n_pages and n_pages > 0) else total
    page_stats = []
    for i in range(min(limit, total)):
        txt = doc[i].get_text()
        imgs = doc[i].get_images()
        page_stats.append({"page": i, "chars": len(txt.strip()), "images": len(imgs)})
    out = {
        "pdf": str(pdf_path),
        "name": Path(pdf_path).name,
        "pages": total,
        "outline": doc.get_toc()[:30],
        "page_stats": page_stats,
        "flags": [s["page"] for s in page_stats if s["chars"] < 50 and s["images"] > 0],
        "low_text_pages": [s["page"] for s in page_stats if s["chars"] < 50],
    }
    doc.close()
    return out


# ---------------------------------------------------------------------------
# 工具 2：查目录（scan_index）—— 像人翻书目录一样，看关键词出现在哪些页
# 这是「人式读文档」的第 1 步：先定位大概位置，再决定看哪页。
# 索引优先：有 ingest 预建索引（.cache/<sha16>/index.json）先查索引（不打开 PDF）；
#           无索引才降级为即时扫描文本层（会话内只全量解析一次）。
# ---------------------------------------------------------------------------
def scan_index(pdf_path, keywords, context: int = 120, max_per_keyword: int = 8,
               pages: list | None = None) -> list:
    """查目录：关键词命中哪些页 + 每页讲什么（title/type）+ 上下文。

    用法（AI 判断关键词）：
        scan_index(pdf, ["headcount", "job reduction", "9,000"])
        scan_index(pdf, ["appointed", "resigned", "effective"])
    返回（每条 = 一个"目录条目"）：
        [{"keyword": str, "page": int(1基), "page0": int(0基),
          "excerpt": 命中处上下文, "title": 该页标题/None, "type": 页类型/None,
          "source": "index"(查预建索引) | "live"(即时扫描)}, ...]
    像人翻目录：一眼看到"关键词在这几页、每页大概讲什么"，再决定翻开哪页。
    """
    idx = _find_index(pdf_path)
    if idx is not None and (idx.get("page_texts") or idx.get("pages")):
        return _scan_via_index(idx, keywords, max_per_keyword)
    return _scan_live(pdf_path, keywords, context, max_per_keyword, pages)


# ---------------------------------------------------------------------------
# 工具 4：搜索 —— 关键词检索（辅助；主定位用 scan_index）。AI 可反复调用、换词。
# ---------------------------------------------------------------------------
def search_pages(pdf_path, query: str, topk: int = 10, cache: dict | None = None,
                 pages: list | None = None) -> list:
    """在 PDF 的文本层做关键词检索，返回按相关度排序的页。

    参数：
        query  —— 由 AI 构造（可多轮换词：'leadership positions changed' →
                  'appointed director' → 'appointment' …）
        pages  —— 可选，限定检索的页范围（配合 inspect 结果）
    返回：
        [{"page": int(0基), "score": float, "excerpt": 片段, "n_chars": int}, ...]
    """
    import fitz
    doc = fitz.open(str(pdf_path))
    ql = query.lower()
    qwords = set(re.findall(r"[a-z0-9]+", ql))
    if not qwords:
        doc.close()
        return []

    def stem_match(word: str, text_low: str) -> bool:
        """词干匹配：appoint 命中 appointed/appointment/appointing；数字/短词用前缀>=4。"""
        if word in text_low:
            return True
        if len(word) >= 5:
            return word[:5] in text_low
        if len(word) == 4:
            return word[:4] in text_low
        return False

    scores = []
    total = len(doc)
    page_range = pages if pages is not None else range(total)
    for i in page_range:
        if not (0 <= i < total):
            continue
        txt = doc[i].get_text()
        low = txt.lower()
        # 加权：完整词命中 > 词干命中；完整短语命中额外加权
        hit = 0.0
        for w in qwords:
            if w in low:
                hit += 1.0
            elif stem_match(w, low):
                hit += 0.6
        if hit == 0:
            continue
        score = hit / len(qwords)          # 覆盖率
        if query.lower() in low:           # 完整短语命中加权
            score += 0.5
        scores.append({"page": i, "score": round(score, 3),
                       "n_chars": len(txt),
                       "excerpt": re.sub(r"\s+", " ", txt[:400])})
    doc.close()
    scores.sort(key=lambda x: -x["score"])
    return scores[:topk]


# ---------------------------------------------------------------------------
# 工具 5：读文本 —— 挑了某页，先快速瞄一眼文本层（快、零成本）
# ---------------------------------------------------------------------------
def read_text(pdf_path, page: int, max_chars: int = 8000) -> str:
    """读指定页（0基）的文本层。若文本为空或极短，AI 应改用 read_vision 看图。"""
    import fitz
    doc = fitz.open(str(pdf_path))
    if not (0 <= page < len(doc)):
        doc.close()
        return "(页号越界)"
    t = doc[page].get_text().strip()
    doc.close()
    if len(t) > max_chars:
        t = t[:max_chars] + f"\n...(截断，共{len(t)}字符)"
    return t


# ---------------------------------------------------------------------------
# 工具 6：看图（我的眼睛）—— 渲染该页，让 VLM 按 AI 给的指令处理。
# 这是"视觉能力"的唯一入口：乱码朗读 / 图纸标注 / 表格数字 / OCR / 针对问题作答
# ---------------------------------------------------------------------------
def read_vision(pdf_path, page: int, instruction: str,
                cache_dir: Path | None = None, dpi: int = 150,
                client=None) -> str:
    """渲染指定页为 PNG，上传后让 VLM 看图并按 instruction 处理。

    instruction 完全由 AI 构造，例如：
      - "这页文本层是乱码，请逐字朗读整页内容（含表格数字）"
      - "这是一张工程图纸，请列出所有文字标注、尺寸/数值（带单位）、图例"
      - "这是表格页，请输出完整 Markdown 表格，数字逐格核对"
      - "请只看这张图回答：报告中披露的 X 指标数值是多少？"
    返回 VLM 的文本结果。渲染 PNG 缓存于 cache_dir（默认 scripts/.cache/tools/）。
    """
    import fitz
    from ds_client import DSClient

    cache_dir = cache_dir or (HERE.parent / ".cache" / "tools")
    cache_dir.mkdir(parents=True, exist_ok=True)
    png = cache_dir / f"{Path(pdf_path).stem}_p{page:04d}.png"

    doc = fitz.open(str(pdf_path))
    if not (0 <= page < len(doc)):
        doc.close()
        return "(页号越界)"
    pix = doc[page].get_pixmap(dpi=dpi)
    pix.save(str(png))
    doc.close()

    if client is None:
        client = DSClient()
    file_id = client.upload_image(str(png))
    blocks = [
        {"type": "text", "text": instruction},
        {"type": "file", "file_id": file_id},
    ]
    data, _ = client.chat(blocks, thinking=False, max_tokens=4096, retries=3)
    return (data or "").strip()


# ---------------------------------------------------------------------------
# 工具 7：自检 —— 出答案前的核验（防幻觉，作为工具而非强制流程）
# ---------------------------------------------------------------------------
def verify_quote(quote, evidence_pages, kind, value) -> tuple[bool, str]:
    """核验：quote 是否真实存在于某证据页、数字是否与 value 匹配。

    evidence_pages: [(page_index, page_text), ...]（AI 从 read_text/read_vision 收集）
    返回 (ok, reason)。ok=False 时 AI 应修正答案或改答 N/A。
    这是防幻觉自检，由 AI 在收敛前主动调用，不是流水线强制步骤。
    """
    if not quote or not isinstance(quote, str) or len(quote.strip()) < 12:
        return False, "quote 缺失或过短"

    def norm(s):
        return re.sub(r"\s+", " ", re.sub(r"[*_|#`]", " ", str(s).lower())).strip()

    nq = norm(quote)
    probe = nq[:70]
    hit = None
    for p, txt in evidence_pages:
        if probe and probe in norm(txt):
            hit = p
            break
    if hit is None:
        return False, "quote 不在任何证据页中（疑似编造）"

    if kind == "number":
        try:
            v = abs(float(value))
        except (TypeError, ValueError):
            return False, "value 非数值"
        nums = [float(m.replace(",", "")) for m in
                re.findall(r"-?\d[\d,]*(?:\.\d+)?", nq)]
        for c in nums:
            c = abs(c)
            if c == 0:
                continue
            for scale in (1, 1e3, 1e6, 1e9):
                if abs(c * scale - v) <= max(1.0, v * 0.01):
                    return True, f"已核验 @page {hit}"
        return False, f"quote 中无匹配 {value} 的数字（quote 数字={nums[:5]}）"
    return True, f"已核验 @page {hit}"


# ---------------------------------------------------------------------------
# 自检（无流水线逻辑，仅验证工具本身可用）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    pdf = Path(r"D:\PageIndex-deepseek\enterprise-rag-challenge-main\round2\pdfs")
    sample = next(pdf.glob("da663e46*.pdf"))
    info = inspect_pdf(sample, n_pages=5)
    print(f"inspect: {info['name']} 共{info['pages']}页, 抽样5页flags={info['flags']}")
    hits = search_pages(sample, "patents portfolio", topk=3)
    print(f"search 'patents portfolio': {[h['page'] for h in hits]}")
    txt = read_text(sample, 15)
    print(f"read_text p15: {len(txt)}字符, 开头={txt[:50]!r}")
    ok, why = verify_quote("Year-end patent portfolio 2,300", [(15, txt)], "number", 2300)
    print(f"verify: {ok} ({why})")
    print("工具自检完成 ✓（read_vision 需 API，未在自检中调用）")
