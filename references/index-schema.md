# 索引结构与提示词

## index.json（.cache/<sha256前16位>/index.json）

```jsonc
{
  "pdf": "绝对路径",
  "pdf_name": "test.pdf",
  "sha256": "全文哈希，缓存键",
  "model": "deepseek-v4-flash-vision-exp",
  "created": "ISO 时间",
  "total_pages": 120,          // PDF 实际页数
  "pages_indexed": 120,        // 已索引页数（--limit 时小于 total_pages）
  "partial": false,            // 是否只索引了部分
  "pages": [                   // 每页一条，按物理页码排序
    {
      "page": 6,
      "type": "封面|版权|目录|序言|正文|附录|参考文献|索引|空白|其他",
      "headings": [{"level": 1, "text": "§02 它被允许碰什么"}],  // 目录/封面页为空
      "summary": "60字以内中文摘要",
      "keywords": ["审批", "权限"],
      "has": {"figure": false, "table": false, "code": false, "formula": false}
    }
  ],
  "page_texts": ["...", "..."],   // 每页文本层（PyMuPDF），供本地检索与深读
  "outline": [{"level": 1, "text": "§00 序章", "page": 4}]  // 由 headings 派生
}
```

同目录下：
- `pages/p0001.png ...` —— 每页渲染图（150dpi，单边 ≤3600px）
- `files.json` —— `{"1": "file-api-..."}` 页码 → Files API file_id（断点续传依据）

## 核心提示词

### 批量页索引（ingest.py `INDEX_SYSTEM`，关推理 + JSON，保留）
要求对每页输出 page/type/headings/summary/keywords/has，页码以 `[第N页]` 标签为准，
必须全覆盖不遗漏。用户消息为 `[第N页]` 文本块与 file 块交错 + 末尾一句范围确认。
（ingest.py 为可选的一次性建索引工具，见 README 第五节）

> 注：原 ask.py 的 `RERANK_SYSTEM` / `DEEP_SYSTEM`（视觉精排 + 深读回答）属于已移除的
> 程序化答题流水线，不再使用；其人式等价物是 `agentic_tools.read_vision`，由 AI 临场
> 构造指令看图。

## 产出文件（运行目录下的 pdf-vision-out/）

- `<书名>_p<N>.png` —— 指定页图片（`show.py` 高清渲染；人式流程由 `read_vision`
  在 `.cache/tools/` 渲染、按需展示）
- `<书名>_<问题>.html` —— 自包含预览：答案全文 + 全部引用页原图（base64 内嵌，
  双击浏览器打开，无需联网）；由人式流程按需生成

## 缓存与失效

- 缓存键 = PDF 内容 sha256 前 16 位；PDF 改动即另起缓存。
- `ingest.py --force` 重建索引（复用已上传文件）；`--clean` 删本地缓存。
- Files API 文件默认永久有效；`read_vision` / `ingest` 上传前会校验 file_id，失效自动重传。
- 想彻底清理服务端文件：`client.files.list()` 后对不再被任何 files.json 引用的
  `file-api-...` 调 `client.files.delete(fid)`。

## v2 新增字段（--route auto 模式）

```jsonc
"route": "auto",
"pages": [{ "page": 1, "type": "...", "summary": "...",
            "source": "text | vlm",      // vlm = VLM 转录来源
            "route_label": "TEXT|TABLE|GRAPHIC|SCAN|GARBLED" }],
"page_texts": ["文本页直录文本 或 乱码/图纸/扫描页的 VLM 转录文本"]
```

- `page_texts` 对 GARBLED/SCAN/GRAPHIC 页存放**转录文本**（不再是 PyMuPDF 原始垃圾/空），
  因此这些页可被本地检索命中（消除盲区）
- 转录由 `scripts/transcribe.py` 完成；渲染 PNG 与 file_id 复用 `.cache/<sha>/`
