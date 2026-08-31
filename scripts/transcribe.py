"""transcribe.py —— VLM 视觉转录（补盲区核心）

对 router 标出的 GARBLED / SCAN / GRAPHIC 页，渲染 → 上传 → VLM 看图转录，
产出完整可检索文本，回填索引的 page_texts（原始文本层不可用的页从此可被检索）。

按页类型切换提示词：
    GARBLED  乱码页：渲染正常，VLM 朗读整页（本质是读一张干净的图）
    SCAN     扫描页：VLM OCR 整页
    GRAPHIC  图纸页：提取 标注文字/尺寸数值/图例/内容描述 → 结构化

用法（库函数，供 ingest.py 调用）：
    from transcribe import transcribe_pages
    text_map = transcribe_pages(client, pdf_path, {page: label}, cache_dir)
"""
import json
import sys
import time
from pathlib import Path

import fitz

from ds_client import DSClient, ChatError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 转录提示词
PROMPT_GARBLED = """你是一台 PDF 页面阅读器。你会收到一页或多页 PDF 的渲染图（这些页的文本层损坏，但渲染正常）。
请把每一页内容【逐字、完整】读出来，包括正文、表格数字。
要求：
1. 完整转录，不要总结、不要遗漏数字和表格内容
2. 表格用 Markdown 表格格式输出
3. 多页时每页输出前加一行【[第N页]】标记（N 为给出的页码）
4. 只输出转录文本，不要任何解释"""

PROMPT_SCAN = """你是一台 OCR 引擎。这是一页或多页扫描件/图片型 PDF 页。
请把每一页内容【逐字、完整】转录出来，包括标题、正文、表格数字、标注。
要求：表格用 Markdown 表格格式输出；多页时每页输出前加一行【[第N页]】标记（N 为给出的页码）；
只输出转录文本，不要任何解释"""

PROMPT_GRAPHIC = """你看到的是工程图纸 / 示意图 / 图表页面。请仔细看图并结构化转录：
1. texts:  图中所有文字标注（标题、部件名、标签、图例文字），逐字列出
2. numbers: 图中所有数字/尺寸/参数（数值+单位），逐一列出
3. desc:   用一句话描述这张图的内容（这是什么图/表/示意图）
输出 JSON：{"texts": ["..."], "numbers": ["...", "..."], "desc": "..."}
只输出 JSON。【图片】"""

PROMPTS = {"GARBLED": PROMPT_GARBLED, "SCAN": PROMPT_SCAN, "GRAPHIC": PROMPT_GRAPHIC}


def _render_missing(pdf: Path, pages: list, pages_dir: Path, dpi: int = 150):
    """只渲染缺失的 PNG（沿用 ingest 的渲染参数，单边≤3600px）。"""
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf))
    out = {}
    for p in pages:
        png = pages_dir / f"p{p:04d}.png"
        if not png.exists():
            page = doc[p - 1]
            long_side_in = max(page.rect.width, page.rect.height) / 72
            d = max(72, min(dpi, int(3600 / long_side_in)))
            page.get_pixmap(dpi=d).save(str(png))
        out[p] = png
    doc.close()
    return out


def _upload(client: DSClient, page_pngs: dict, files_json: Path, workers: int = 4):
    """上传缺失页图（files.json 断点续传）。返回 {页码str: file_id}。"""
    import concurrent.futures
    files_map = json.loads(files_json.read_text("utf-8")) if files_json.exists() else {}
    todo = {p: png for p, png in page_pngs.items() if str(p) not in files_map}
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(client.upload_image, str(png)): p for p, png in todo.items()}
            for fut in concurrent.futures.as_completed(futs):
                files_map[str(futs[fut])] = fut.result()
        files_json.write_text(json.dumps(files_map), "utf-8")
    return files_map


def _normalize_graphic(raw) -> str:
    """GRAPHIC 转录 JSON → 可检索文本。"""
    if not isinstance(raw, dict):
        return str(raw or "")
    parts = []
    if raw.get("texts"):
        parts.append("标注文字: " + " | ".join(str(t) for t in raw["texts"]))
    if raw.get("numbers"):
        parts.append("尺寸数值: " + " | ".join(str(n) for n in raw["numbers"]))
    if raw.get("desc"):
        parts.append("内容描述: " + str(raw["desc"]))
    return "；".join(parts)


def transcribe_pages(client: DSClient, pdf: Path, labels: dict,
                     cache_dir: Path, batch: int = 10, dpi: int = 150,
                     workers: int = 4) -> dict:
    """转录 labels 指定的页（{page: GARBLED|SCAN|GRAPHIC}）。

    返回 {page: 转录文本}。单页失败不影响其余（记入失败列表）。
    缓存：渲染 PNG 与 files.json 落在 cache_dir，重复运行零成本。
    """
    if not labels:
        return {}
    pages_dir = cache_dir / "pages"
    pngs = _render_missing(pdf, sorted(labels), pages_dir, dpi)
    files_map = _upload(client, pngs, cache_dir / "files.json", workers)

    out, failed = {}, []
    ordered = sorted(labels.items())          # 按页序，天然同类型相邻
    t0 = time.time()
    for i in range(0, len(ordered), batch):
        chunk = ordered[i:i + batch]
        blocks = []
        for p, lab in chunk:
            blocks.append({"type": "text", "text": f"[第{p}页] {lab}"})
            blocks.append({"type": "file", "file_id": files_map[str(p)]})
        blocks.append({"type": "text", "text": f"请处理第{chunk[0][0]}页到第{chunk[-1][0]}页。"})
        labs = {lab for _, lab in chunk}
        try:
            if labs == {"GRAPHIC"}:
                # GRAPHIC：JSON 结构化（json_mode 可用，提示词含 json 字样）
                data, _ = client.chat_json(blocks, system=PROMPTS["GRAPHIC"],
                                           thinking=False, max_tokens=8192)
                # 可能是 {页: {...}} 或 {"pages":[...]} 结构
                recs = data.get("pages") if isinstance(data, dict) else data
                for p, lab in chunk:
                    rec = None
                    if isinstance(data, dict):
                        rec = data.get(str(p)) or data.get(p)
                    if rec is None and isinstance(recs, dict):
                        rec = recs.get(str(p))
                    if isinstance(rec, dict):
                        out[p] = _normalize_graphic(rec)
                    elif isinstance(rec, str):
                        out[p] = rec
                    else:
                        failed.append(p)
            else:
                # GARBLED/SCAN：纯文本转录（不能用 json_mode——prompt 无 json 字样）
                text, _ = client.chat(blocks, system=PROMPTS["GARBLED"],
                                      thinking=False, max_tokens=8192)
                # 按 [第N页] 标记拆分
                import re
                segs = re.split(r"\[第(\d+)页\]", text)
                # segs: [前缀, 页码, 内容, 页码, 内容, ...]
                for j in range(1, len(segs), 2):
                    try:
                        pno = int(segs[j])
                    except ValueError:
                        continue
                    content = segs[j + 1].strip() if j + 1 < len(segs) else ""
                    if content:
                        out[pno] = content
                    else:
                        failed.append(pno)
                # 单页且未带标记：整体当作该页文本
                if len(chunk) == 1 and chunk[0][0] not in out and text.strip():
                    out[chunk[0][0]] = text.strip()
        except ChatError as e:
            failed.extend(p for p, _ in chunk)
            print(f"  [transcribe] 批 {chunk[0][0]}-{chunk[-1][0]} 失败: {e}", flush=True)
            continue
        print(f"  [transcribe] p{chunk[0][0]}-{chunk[-1][0]} ok "
              f"({time.time()-t0:.0f}s)", flush=True)

    if failed:
        print(f"  [transcribe] 失败 {len(failed)} 页: {failed[:10]}", flush=True)
    return out


if __name__ == "__main__":
    # 自检用法：python transcribe.py <pdf> p1,p2,p3 GARBLED
    pdf = Path(sys.argv[1])
    pages = {int(x) for x in sys.argv[2].split(",")}
    lab = sys.argv[3] if len(sys.argv) > 3 else "GARBLED"
    client = DSClient()
    cache = Path(__file__).resolve().parent / ".cache" / "transcribe_test"
    text_map = transcribe_pages(client, pdf, {p: lab for p in pages}, cache)
    for p, t in text_map.items():
        print(f"=== p{p} 转录前 300 字 ===")
        print(t[:300])
