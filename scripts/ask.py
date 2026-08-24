"""ask.py —— 基于 ingest 构建的视觉索引回答用户对 PDF 的提问。

流程（共 2 次 API 调用 + 本地零成本粗筛）：
1. 本地粗筛：问题分词（中文 bigram + 英文词）× 页级 TF-IDF 打分 → 候选 top12
2. 视觉精排（关推理，1 次调用）：候选页截图+摘要给模型，选出最相关的 top-k 页
3. 深读回答（开推理，1 次调用）：命中页±1 邻页的截图 + 重点页文本层 → 带页码引用的回答
4. 把命中页图片复制到 ./pdf-vision-out/，打印路径供 agent 用 Read 工具展示给用户

用法：
  python ask.py <pdf> "问题" [--top 4] [--cands 12] [--no-images]
"""
import argparse
import base64
import hashlib
import html
import json
import math
import re
import shutil
import sys
from pathlib import Path

from ds_client import DSClient, ChatError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parents[1]
CACHE_ROOT = SKILL_DIR / ".cache"
OUT_DIR = Path("pdf-vision-out")

STOP_TERMS = {"什么", "怎么", "哪些", "如何", "为什么", "这个", "那个", "请问", "告诉",
              "一下", "资料", "文档", "书里", "书中", "里面", "文中", "介绍", "描述",
              "the", "a", "an", "of", "in", "is", "what", "how", "which", "and", "to"}

RERANK_SYSTEM = """你是 PDF 问答的页面筛选器。用户提出一个问题，你会看到若干候选页的页码、摘要和页面截图。
请选出最能用于回答该问题的页面，最多选 {k} 页，按相关性从高到低排列。
不要为了凑数而选无关页面；如果一个都不相关，返回空列表。
输出 JSON：{{"pages": [{{"page": 6, "reason": "10字以内理由"}}]}}，只输出 JSON。"""

DEEP_SYSTEM = """你是深度阅读助手。你会看到与用户问题相关的 PDF 页面截图（含标题为 [第N页] 的标签），以及重点页的文本层。
请认真阅读图像中的全部内容，包括图表、表格、代码块和版式信息。
回答要求：
1. 用中文回答，准确、具体，先给结论再给要点
2. 每个关键论点后标注来源物理页码，格式如 [第6页]
3. 若页面内容不足以回答问题，明确说明缺什么，不要编造
4. 控制在 600 字以内"""


def pdf_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_index(pdf: Path):
    sha = pdf_sha256(pdf)
    index_path = CACHE_ROOT / sha[:16] / "index.json"
    if not index_path.exists():
        sys.exit(f"[error] 未找到索引，请先运行: python ingest.py \"{pdf}\"")
    return json.loads(index_path.read_text("utf-8")), index_path.parent


def terms_of(question: str):
    """问题分词：英文/数字词 + 中文 bigram（单字中文运行保留）。"""
    terms = set(re.findall(r"[A-Za-z0-9_.]{2,}", question.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", question):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms - STOP_TERMS


def score_pages(index, question):
    """本地 TF-IDF 粗筛。summary/keywords/headings 权重高于正文文本层。"""
    pages = [r for r in index["pages"] if r["type"] != "空白"]
    terms = terms_of(question)
    if not terms:
        return [r["page"] for r in pages]

    df = {}
    docs = {}
    for r in pages:
        parts = []
        for h in r["headings"]:
            parts.extend([h["text"]] * 3)
        parts.extend([r["summary"]] * 3)
        parts.extend(r["keywords"] * 3)
        parts.append(index["page_texts"][r["page"] - 1])
        doc = "\n".join(parts)
        docs[r["page"]] = doc
        for t in terms:
            if t in doc:
                df[t] = df.get(t, 0) + 1

    n = len(pages)
    scored = []
    for page_no, doc in docs.items():
        s = 0.0
        for t in terms:
            tf = doc.count(t)
            if tf:
                idf = math.log(1 + n / df[t])
                s += idf * min(tf, 8)
        scored.append((page_no, s))
    scored.sort(key=lambda x: -x[1])
    return [p for p, s in scored if s > 0]


def ensure_files(client: DSClient, cache_dir: Path, files_map: dict, pages):
    """校验所需页的 file_id 仍然有效，失效则重传并更新 files.json。"""
    files_json = cache_dir / "files.json"
    changed = False
    for p in pages:
        fid = files_map.get(str(p))
        if not fid or not client.file_exists(fid):
            png = cache_dir / "pages" / f"p{p:04d}.png"
            files_map[str(p)] = client.upload_image(str(png))
            changed = True
            print(f"[files] re-uploaded page {p}", flush=True)
    if changed:
        files_json.write_text(json.dumps(files_map, ensure_ascii=False, indent=1), "utf-8")


def rerank(client, index, files_map, cand, k, question):
    blocks = []
    for p in cand:
        r = index["pages"][p - 1]
        blocks.append({"type": "text",
                       "text": f"[第{p}页] 摘要：{r['summary']}；标题：{'；'.join(h['text'] for h in r['headings'])}"})
        blocks.append({"type": "file", "file_id": files_map[str(p)]})
    blocks.append({"type": "text", "text": f"用户问题：{question}\n请选出最多 {k} 个最相关的页面。"})
    try:
        data, _ = client.chat_json(blocks, system=RERANK_SYSTEM.format(k=k),
                                   thinking=False, max_tokens=600, temperature=0)
    except ChatError as e:
        print(f"[rerank] failed ({e}), falling back to local ranking", flush=True)
        return cand[:k]
    picked = []
    for item in data.get("pages") or []:
        try:
            p = int(item["page"])
            if p in cand and p not in picked:
                picked.append(p)
        except (KeyError, TypeError, ValueError):
            continue
    return picked[:k] or cand[:k]


def deep_read(client, index, files_map, pages, question):
    blocks = []
    for p in pages:
        blocks.append({"type": "text", "text": f"[第{p}页]"})
        blocks.append({"type": "file", "file_id": files_map[str(p)]})
    text_parts = [f"<第{p}页>\n{index['page_texts'][p - 1]}\n</第{p}页>" for p in pages]
    blocks.append({"type": "text",
                   "text": "重点页文本层：\n" + "\n".join(text_parts) + f"\n\n用户问题：{question}"})
    # 深读开推理（质量优先）；reasoning 计入 max_tokens，因此给足预算
    text, finish = client.chat(blocks, system=DEEP_SYSTEM, thinking=True,
                               max_tokens=8192, retries=3)
    if finish == "length":
        text += "\n\n（注：回答因长度限制被截断）"
    return text


def md_inline_to_html(text: str) -> str:
    """轻量 markdown 内联转换（加粗/斜体/行内代码/列表符），供 HTML 预览页使用。
    先做 HTML 转义再替换，产出的标签不会被再解析。"""
    s = html.escape(text)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", s)
    s = re.sub(r"^[-•]\s+", "• ", s, flags=re.MULTILINE)
    return s


def write_html_preview(pdf_name, question, answer, cited, cache_dir: Path) -> Path:
    """生成自包含 HTML 预览：答案 + 全部引用页原图（base64 内嵌，双击浏览器即看，无需联网）。"""
    stem = pdf_name.rsplit(".", 1)[0]
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "_", question).strip("_")[:24] or "answer"
    head = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{html.escape(question[:60])}</title>
<style>
body{{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:920px;margin:0 auto;padding:24px;color:#222}}
.q{{font-size:15px;color:#555;margin-bottom:8px}}
.answer{{white-space:pre-wrap;background:#f6f7f9;padding:16px 20px;border-radius:10px;font-size:15px;line-height:1.7}}
.answer code{{background:#e8e8ec;padding:1px 5px;border-radius:3px;font-size:0.9em}}
h2{{margin-top:36px;border-bottom:2px solid #e8e8e8;padding-bottom:6px}}
h3{{color:#666;font-weight:600;margin-bottom:0}}
img{{width:100%;border:1px solid #ddd;border-radius:6px;margin:6px 0 28px}}
</style></head><body>
<h1>{html.escape(pdf_name)}</h1>
<div class="q">问题：{html.escape(question)}</div>
<div class="answer">{md_inline_to_html(answer)}</div>
<h2>引用页原图</h2>"""
    parts = [head]
    for p in cited:
        png = cache_dir / "pages" / f"p{p:04d}.png"
        if not png.exists():
            continue
        b64 = base64.b64encode(png.read_bytes()).decode()
        parts.append(f'<h3>第{p}页</h3><img alt="第{p}页" src="data:image/png;base64,{b64}">')
    parts.append("</body></html>")
    out = OUT_DIR / f"{stem}_{slug}.html"
    out.write_text("".join(parts), "utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description="Ask a question about an indexed PDF")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("question")
    ap.add_argument("--top", type=int, default=4, help="精排选出的深读页数")
    ap.add_argument("--cands", type=int, default=12, help="本地粗筛候选页数")
    ap.add_argument("--no-images", action="store_true", help="不复制页面图片")
    args = ap.parse_args()

    pdf = args.pdf.resolve()
    index, cache_dir = load_index(pdf)
    n_indexed = index["pages_indexed"]
    files_map = json.loads((cache_dir / "files.json").read_text("utf-8"))
    print(f"[ask] {index['pdf_name']} 索引 {n_indexed}/{index['total_pages']} 页", flush=True)

    ranked = score_pages(index, args.question)[:args.cands]
    if not ranked:
        print("[ask] 本地检索无命中，回退到前若干索引页", flush=True)
        ranked = [r["page"] for r in index["pages"][:args.cands]]
    print(f"[ask] 本地粗筛 top{min(len(ranked), 8)}: {ranked[:8]}", flush=True)

    client = DSClient()
    ensure_files(client, cache_dir, files_map, ranked)

    picked = rerank(client, index, files_map, ranked, args.top, args.question)
    print(f"[ask] 视觉精排选中: {picked}", flush=True)

    read_pages = sorted({q for p in picked for q in (p - 1, p, p + 1)
                         if 1 <= q <= n_indexed})  # 命中页 ±1 邻页
    ensure_files(client, cache_dir, files_map, read_pages)
    print(f"[ask] 深读页（含邻页）: {read_pages}", flush=True)

    answer = deep_read(client, index, files_map, read_pages, args.question)

    cited = sorted({int(m) for m in re.findall(r"第(\d+)页", answer)})
    print("\n=== 答案 ===")
    print(answer)

    print("\n=== 引用页 ===")
    print("、".join(f"第{p}页" for p in cited) if cited else "（回答中无显式页码引用）")

    if not args.no_images and cited:
        print("\n=== 页面图片 ===")
        OUT_DIR.mkdir(exist_ok=True)
        for p in cited:
            src = cache_dir / "pages" / f"p{p:04d}.png"
            if src.exists():
                dst = OUT_DIR / f"{index['pdf_name'].rsplit('.', 1)[0]}_p{p}.png"
                shutil.copyfile(src, dst)
                print(dst.resolve())
        preview = write_html_preview(index["pdf_name"], args.question, answer, cited, cache_dir)
        print("\n=== 预览（双击用浏览器打开：答案 + 全部引用页原图） ===")
        print(preview.resolve())


if __name__ == "__main__":
    main()
