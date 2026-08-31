---
name: deepseek-v4-flash-vision-rag
description: 基于DeepSeek视觉大模型（deepseek-v4-flash-vision-exp）的PDF深度问答（人式读文档 Vision RAG）。当用户提到 PDF、文档、资料、报告、论文、电子书、手册、说明书、图纸、表格，并想提问、查找、搜索、总结或理解其内容，或问"在哪一页"、"引用原文"、要页面截图时，使用本skill。支持扫描版、图表、图纸、表格、代码的视觉理解；回答带物理页码引用，并把命中页原图展示给用户。
---

# DeepSeek V4-Flash Vision RAG（人式读文档）

## 核心原则：你就是那个读文档的人

**不要写程序、不要跑流水线、不要写批量脚本。你是一个专业的文档阅读者，手里有一套工具，像人一样翻开文档、看目录、看页、找到答案。**

```
查目录 → 视觉看页 → 没找到就深挖 → 给答案（一次一次来，务必得到答案）
```

- 每一步（看哪页、看什么、要不要深挖）由你当下判断，没有预定义流程
- 工具只提供能力：查目录、看图、读文本、自检
- 真实使用是一次一题，每题认真做完，绝不敷衍

---

## 工作流（像人一样读文档）

### 第 1 步：查目录（scan_index）—— 先知道"大概在哪"

人拿到一本书，先翻目录页——目录是**预先整理好的**（标题、主题、页码）。你拿到 PDF，同样先查**预建索引**（`.cache/<sha>/index.json`，由 ingest.py 预建：每页转录文本 + 页标题/类型/摘要）：

```python
entries = T.scan_index(pdf, ["headcount reduction", "9,000"])   # 关键词由你想
# 返回: [{keyword, page(1基), excerpt, title, type, source}, ...] 按页码排列
# source="index" = 查预建索引（秒回，不打开 PDF）
# source="live"  = 无索引时降级即时扫描文本层（会话内只全量解析一次）
```

- **索引优先**：有预建索引先查索引（快、且转录让乱码/扫描页也可检索）；
  没有索引才即时扫描——**每个 PDF 同一会话内只全量解析一次**，换词不重复扫。
- **关键词由你判断**：题目说 "employees let go"，你要想到年报里可能写
  "headcount reduction / job cuts / restructuring / redundancies"
- 题目说 "leadership positions changed"，要想到 "appointed / resigned / effective"
- 看目录结果，你就能判断：大概在第几页、那页讲什么（title/type）→ 决定翻开哪页

> 注：`ingest.py --route auto` 可给新 PDF 预建索引（人式流程的标准前置步骤；
> 预建后 scan_index 走索引，快且覆盖扫描页）。PDF 带书签目录时也可直接用。

### 第 2 步：视觉看页（read_vision）—— 翻开那一页，亲眼看看

```python
txt = T.read_vision(pdf, page0, "逐字读出这一页关于 X 的内容，包括数字")  # page0 是 0 基
```

- **指令由你构造**：你决定要在这页找什么
  - "这一页是否提到任命？逐字列出职位和姓名"
  - "逐字读出人员削减的句子，特别是数字"
  - "这是不是静态名单/模板页？有没有变动？"
- 也可以先用 `read_text` 快速瞄一眼文本层（快、零成本），再决定要不要看图

### 第 3 步：深度查看 —— 没找到就挖

人找不到就翻邻页、翻章节深处、回看目录。你也一样：

- **换关键词再扫目录**：`scan_index(pdf, ["reduction of almost", "full-time positions"])`
- **翻邻页**：找到线索页后，看它前后页（上下文往往在邻页）
- **找数字/动词**：number 题直接扫数字，boolean 题扫 "announced/changes/effective"

### 第 4 步：收敛 —— 务必得到答案

- **找到了** → 给出答案 + 页码（你亲眼看到的，不是程序推的）
- **确实没有**（穷尽目录与邻页后）→ 明确说"查了目录哪些关键词、看了第 X-Y 页，
  没有找到"，再答 N/A
- 绝不因为"一次没搜到"就放弃，也不编造不存在的引用

---

## 工具索引（scripts/agentic_tools.py，你自主调用）

| 工具 | 用途 | 什么时候用 |
|---|---|---|
| `scan_index(pdf, keywords)` | **查目录**：优先查预建索引（.cache），无则即时扫描；返回页码+页标题/类型+上下文 | 第 1 步，定位大概位置（反复换词调用） |
| `read_vision(pdf, page0, instr)` | **看图（你的眼睛）**：渲染该页，按你的指令提取 | 第 2 步，视觉查看 |
| `read_text(pdf, page0)` | 快速瞄一眼文本层（零成本） | 先看文字再决定要不要看图 |
| `search_pages(pdf, query)` | 辅助检索（带词干匹配） | 目录扫不到时兜底 |
| `verify_quote(quote, evidence, kind, value)` | 自检：引用真实存在于页面 | 出答案前核验 |

---

## 决策规则（提示性，辅助判断）

1. **题目措辞 ≠ 年报措辞**：题问 "employees let go"，年报写 "headcount reduction"；
   题问 "leadership positions changed"，年报写 "appointed/resigned"。扫目录时主动换词。
2. **数字题核对口径**：现金流量表取净额行；多口径并存（adjusted vs reported）时
   取与题面最字面匹配的；注意千/百万单位。
3. **boolean：词出现 ≠ 事件发生**（round2 实测：boolean 是最容易失分的题型，
   系统性错误是"看到相关章节就判 True"）。判定标尺——**必须有"变化"的证据**：
   - ✅ 算 True：与去年对比的数字变化（DPS +10% / dividend 0.52→0.60 且提到 policy）、
     报告期内**实际完成**的并购/收购（"acquisition completed in November 2022"）、
     明确发布的新产品、新设定的目标（payout ratio 目标 40%）、股权注入/再融资
   - ❌ 算 False（常见陷阱）：
     · **政策/举措的"描述"而非"变化"**（有 Dividend Policy 章节 ≠ 政策变化；
       有 ESG 章节 ≠ 新的 ESG 举措）
     · **常规声明**（"we are regularly subject to litigation"、"we invest in R&D"）
     · **集成第三方服务**（接入 Zelle 支付 ≠ 自己发布新产品）
     · **历史引用**（2012 年并购协议）与**可能性讨论**（may/would/could）
     · **提案/提议**（launched a "proposal" ≠ launched a product）
   - 拿不准时按 False——实测 5/6 的 boolean 错题都是"我判 True 但真值 False"
4. **names 题答案在动词附近**：appointed/resigned/effective 所在的页，才是变动
   发生的地方；静态"现任名单"页不是答案。
5. **引用必须真实**：只引用你实际读到的页；页码说清是 1 基还是 0 基。
6. **答 N/A 前穷尽**：换 2-3 组关键词、看邻页、必要时看图；然后说明查过哪里。
7. **多文档比较题**：对每家公司分别走完整流程取数，再统一比较。

---

## 回答规范

- 以你实读到的内容为事实基础，组织成简洁中文回答；保留 `[第N页]` 引用标记
- 答案与引用页不符、或找不到支撑时，如实告诉用户，不要编造
- 展示原图时必须提供用户可自行打开的本地路径：
  1. HTML 预览（首选）：`./pdf-vision-out/<书名>_<问题>.html`
  2. 单页 PNG：`./pdf-vision-out/<书名>_p<N>.png`
- 顺带告诉用户引用页码，方便他翻原 PDF 对照

---

## 实测示范（3 例，证明"人式"有效）

### 例 1：Q30 Datalogic 领导职位变动（真值 Director）
- 查目录：`scan_index(pdf, ["governance", "board of director"])` → 第 38 页"董事会构成"
- 看图第 38 页 → **是静态现任名单**，无变动 → 换词 `scan_index(pdf, ["appointed", "new director"])`
- 深挖：第 68 页 "appoint a new director of the Company, in the person of Pietro Todescato"
- **答案：新任命 Director，第 68 页**（142 分方案的止损项，人式流程答出）

### 例 2：Q67 Trinity 是否提及并购（真值 False）
- 查目录：`scan_index(pdf, ["merger", "acquisition"])` → 第 23、41、57 页
- 看图判断：第 23 页是"**未来可能性讨论**（may/could）"、第 41 页是"**2012 年历史引用**"
- **答案：False**（词出现 ≠ 本年度并购事件）

### 例 3：Q53 Commerzbank 裁员人数（真值 9,000）
- 查目录：`scan_index(pdf, ["headcount reduction", "9,000"])` → 第 106 页只有进展描述
- 深挖：第 4 页 "reduction of almost **9,000** positions was contracted by the end of 2022"
- **答案：9,000，第 4 页**（agentic 裸检索失败，人式流程答出）

---

## 环境

- Python 依赖：`openai`、`pymupdf(fitz)`（本机已装）
- API key：从环境变量 `DEEPSEEK_API_KEY` 或本机 `~/.deepseek_api_key` 文件（首行）读取
- 核心工具（人式流程直接调用）：`scripts/agentic_tools.py`
  - `scan_index(pdf, keywords)` 查目录（**索引优先**：预建索引秒回，无索引降级即时扫描）
    ｜`read_vision(pdf, page0, instr)` 看图（眼睛）
    ｜`read_text(pdf, page0)` 看文本层｜`search_pages(pdf, query)` 兜底检索｜`verify_quote(...)` 自检
  - 依赖 `scripts/ds_client.py`（DeepSeek VLM 客户端，由 `read_vision` 调用）
- **建索引工具（人式流程的标准前置步骤）**：`scripts/ingest.py` → `router.py`（页分类）
  + `transcribe.py`（视觉转录）→ 落盘 `.cache/<sha>/index.json`（每页转录文本 + 页标题/类型/摘要）。
  新 PDF 建议先 ingest 预建，`scan_index` 即可秒查索引、且覆盖乱码/扫描页；无索引时自动降级即时扫描。
- **人式流程才是正确的使用方法**：不写批量代码、不跑 agentic 循环，一次一题，直接调 `agentic_tools` 的工具

## 其他参考

- `references/api-notes.md`：API 细节（推理开关、384 token/图、Files API 限制等坑）
- `references/index-schema.md`：索引/缓存结构
