"""ingest.py —— 为 PDF 构建视觉索引（每份 PDF 只需运行一次，结果缓存复用）。

流程：
1. PyMuPDF 渲染每页为 PNG（150dpi，自动钳制在单边 3600px 内，
   满足"单请求 15+ 图时单边 ≤4096px"的限制；单图 token 封顶 384，无需更高分辨率）
2. 抽取每页文本层（零成本，供本地关键词检索与深读引用）
3. Files API 并发上传所有页图 → file_id 清单（断点续传）
4. 分批（默认 25 页/请求）把页图 file_id 打包给 vision 模型，
   关闭推理 + JSON Output，产出每页结构化记录；截断/失败自动对半拆批重试
5. 由页级 headings 派生全书大纲（不需要 PageIndex 式的页码映射与修复循环）
6. 落盘 .cache/<sha256>/index.json

用法：
  python ingest.py <pdf> [--force] [--limit N] [--batch 25] [--dpi 150] [--workers 6] [--clean]
"""
import argparse
import concurrent.futures
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

from ds_client import DSClient, ChatError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parents[1]
CACHE_ROOT = SKILL_DIR / ".cache"
MAX_SIDE_PX = 3600  # 15+ 图/请求时 API 限制单边 4096px，留余量
PAGE_TYPES = ("封面", "版权", "目录", "序言", "正文", "附录", "参考文献", "索引", "空白", "其他")

INDEX_SYSTEM = """你是一个 PDF 页面索引助手。你会收到一批 PDF 页面图像，每张图像前有一个文本标签标明物理页码（如 [第6页]）。

请对收到的每一页输出一条结构化记录，合并为一个 JSON 对象，格式示例：
{"pages": [{"page": 6, "type": "正文", "headings": [{"level": 1, "text": "§02 它被允许碰什么"}], "summary": "60字以内的中文摘要，概括本页讲了什么", "keywords": ["审批", "权限", "工作区"], "has": {"figure": false, "table": false, "code": false, "formula": false}}]}

字段说明：
- page: 物理页码，必须与图像前的 [第N页] 标签一致
- type: 只能取 封面/版权/目录/序言/正文/附录/参考文献/索引/空白/其他 之一
- headings: 本页正文中出现的标题（含章节号如 §02 或 1.2），level 1 为最高级；目录页和封面页必须为空数组
- summary: 60 字以内中文摘要，写实质内容而不是"本页介绍了..."
- keywords: 3-6 个便于检索的关键词（中文或原文英文术语）
- has: 页面是否包含 插图/表格/代码块/数学公式

必须覆盖收到的每一页，不得遗漏、不得编造未给出的页码。只输出 JSON。"""


def pdf_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render_pages(doc, out_dir: Path, dpi: int):
    """渲染每页 PNG，返回按页码排序的文件列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i, page in enumerate(doc):
        png = out_dir / f"p{i + 1:04d}.png"
        if not png.exists():
            long_side_in = max(page.rect.width, page.rect.height) / 72
            d = max(72, min(dpi, int(MAX_SIDE_PX / long_side_in)))
            page.get_pixmap(dpi=d).save(str(png))
        files.append(png)
    return files


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


def upload_all(client: DSClient, page_files, files_json: Path, workers: int):
    """并发上传页图，断点续传（files.json 已有的页跳过）。返回 {页码str: file_id}。"""
    files_map = json.loads(files_json.read_text("utf-8")) if files_json.exists() else {}
    todo = {i + 1: p for i, p in enumerate(page_files) if str(i + 1) not in files_map}
    if not todo:
        print(f"[upload] all {len(page_files)} pages already uploaded", flush=True)
        return files_map

    t0 = time.time()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(client.upload_image, str(p)): no for no, p in todo.items()}
        for fut in concurrent.futures.as_completed(futs):
            files_map[str(futs[fut])] = fut.result()
            done += 1
            if done % 20 == 0 or done == len(todo):
                save_json(files_json, files_map)  # 边传边存，中断可续
                print(f"[upload] {done}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    return files_map


def normalize_record(rec: dict, page: int) -> dict:
    rec["page"] = page
    if rec.get("type") not in PAGE_TYPES:
        rec["type"] = "其他"
    heads = []
    for h in rec.get("headings") or []:
        if isinstance(h, dict) and str(h.get("text", "")).strip():
            try:
                lvl = max(1, min(6, int(h.get("level", 1))))
            except (TypeError, ValueError):
                lvl = 1
            heads.append({"level": lvl, "text": str(h["text"]).strip()[:120]})
    rec["headings"] = heads
    rec["summary"] = str(rec.get("summary", "")).strip()[:200]
    kws = rec.get("keywords") or []
    rec["keywords"] = [str(k).strip()[:40] for k in kws if str(k).strip()][:8]
    has = rec.get("has") or {}
    rec["has"] = {k: bool(has.get(k, False)) for k in ("figure", "table", "code", "formula")}
    return rec


def batch_blocks(pages, files_map):
    blocks = []
    for p in pages:
        blocks.append({"type": "text", "text": f"[第{p}页]"})
        blocks.append({"type": "file", "file_id": files_map[str(p)]})
    blocks.append({"type": "text",
                   "text": f"请输出第{pages[0]}页到第{pages[-1]}页（共{len(pages)}页）每一页的JSON记录。"})
    return blocks


def index_pages(client: DSClient, pages, files_map):
    """索引一组页。截断/解析失败时对半拆批递归；返回拿到的 {页码: record}。"""
    if not pages:
        return {}
    try:
        data, finish = client.chat_json(batch_blocks(pages, files_map), system=INDEX_SYSTEM,
                                        thinking=False, max_tokens=8192)
    except ChatError as e:
        if len(pages) == 1:
            print(f"[index] page {pages[0]} failed: {e}", flush=True)
            return {}
        mid = len(pages) // 2
        return {**index_pages(client, pages[:mid], files_map),
                **index_pages(client, pages[mid:], files_map)}

    records = {}
    raw = data.get("pages") if isinstance(data, dict) else data
    for r in raw or []:
        try:
            records[int(r["page"])] = normalize_record(r, int(r["page"]))
        except (KeyError, TypeError, ValueError):
            continue

    missing = [p for p in pages if p not in records]
    if finish == "length" and len(pages) > 1:
        mid = len(pages) // 2
        print(f"[index] batch p{pages[0]}-{pages[-1]} truncated, splitting", flush=True)
        records.update(index_pages(client, pages[:mid], files_map))
        records.update(index_pages(client, pages[mid:], files_map))
    elif missing and len(missing) < len(pages):
        # 模型漏了几页，只补漏掉的
        records.update(index_pages(client, missing, files_map))
    return records


def build_outline(pages_records):
    """由页级 headings 派生全书大纲。"""
    outline, seen = [], set()
    for rec in sorted(pages_records, key=lambda r: r["page"]):
        if rec["type"] in ("目录", "封面", "版权", "空白", "索引"):
            continue
        for h in rec["headings"]:
            key = h["text"]
            if key in seen:
                continue
            seen.add(key)
            outline.append({"level": h["level"], "text": key, "page": rec["page"]})
    return outline


def main():
    ap = argparse.ArgumentParser(description="Build vision index for a PDF")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--force", action="store_true", help="重建索引（复用已上传文件）")
    ap.add_argument("--limit", type=int, default=0, help="只索引前 N 页（测试用）")
    ap.add_argument("--batch", type=int, default=25, help="每次请求的页数")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--workers", type=int, default=6, help="上传并发数")
    ap.add_argument("--clean", action="store_true", help="删除该 PDF 的本地缓存后退出")
    args = ap.parse_args()

    pdf = args.pdf.resolve()
    if not pdf.is_file():
        sys.exit(f"PDF not found: {pdf}")

    sha = pdf_sha256(pdf)
    cache_dir = CACHE_ROOT / sha[:16]
    if args.clean:
        if cache_dir.exists():
            for f in cache_dir.glob("*"):
                f.unlink()
            cache_dir.rmdir()
            print(f"[clean] removed {cache_dir}")
        return

    doc = fitz.open(str(pdf))
    total = len(doc)
    n_pages = min(total, args.limit) if args.limit > 0 else total
    index_path = cache_dir / "index.json"

    if index_path.exists() and not args.force:
        try:
            old = json.loads(index_path.read_text("utf-8"))
            if old.get("pages_indexed", 0) >= n_pages:
                print(f"[cache] index exists: {cache_dir}")
                print(f"[cache] {old['pages_indexed']}/{old['total_pages']} pages indexed, use --force to rebuild")
                return
        except json.JSONDecodeError:
            pass

    print(f"[ingest] {pdf.name}: {total} pages, indexing {n_pages}, sha={sha[:16]}", flush=True)
    t0 = time.time()

    pages_dir = cache_dir / "pages"
    page_files = render_pages(doc, pages_dir, args.dpi)[:n_pages]
    texts = [doc[i].get_text() for i in range(n_pages)]
    print(f"[render] {len(page_files)} pages -> {pages_dir} ({time.time() - t0:.0f}s)", flush=True)

    client = DSClient()
    files_map = upload_all(client, page_files, cache_dir / "files.json", args.workers)
    print(f"[upload] {len(files_map)} file_ids ready ({time.time() - t0:.0f}s)", flush=True)

    records = {}
    all_pages = list(range(1, n_pages + 1))
    for i in range(0, n_pages, args.batch):
        chunk = all_pages[i:i + args.batch]
        got = index_pages(client, chunk, files_map)
        records.update(got)
        print(f"[index] {min(len(records), i + len(chunk))}/{n_pages} done "
              f"(p{chunk[0]}-p{chunk[-1]}, {time.time() - t0:.0f}s)", flush=True)

    # 仍缺的页（极少）用文本层兜底
    fallback = 0
    for p in all_pages:
        if p not in records:
            fallback += 1
            records[p] = normalize_record(
                {"summary": texts[p - 1][:150], "type": "其他",
                 "keywords": [], "headings": []}, p)
    if fallback:
        print(f"[index] WARNING: {fallback} pages fell back to text-layer-only", flush=True)

    ordered = [records[p] for p in all_pages]
    index = {
        "pdf": str(pdf),
        "pdf_name": pdf.name,
        "sha256": sha,
        "model": client.model,
        "created": datetime.now().isoformat(timespec="seconds"),
        "total_pages": total,
        "pages_indexed": n_pages,
        "partial": n_pages < total,
        "pages": ordered,
        "page_texts": texts,
        "outline": build_outline(ordered),
    }
    save_json(index_path, index)

    types = {}
    for r in ordered:
        types[r["type"]] = types.get(r["type"], 0) + 1
    print(f"[done] {n_pages} pages indexed in {time.time() - t0:.0f}s -> {index_path}", flush=True)
    print(f"[done] page types: {types}", flush=True)
    print(f"[done] outline entries: {len(index['outline'])}", flush=True)


if __name__ == "__main__":
    main()
