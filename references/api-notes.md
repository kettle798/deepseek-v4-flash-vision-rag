# DeepSeek Vision API 实测笔记

模型 `deepseek-v4-flash-vision-exp`，base_url `https://api.deepseek.com`，OpenAI 兼容。
以下全部经过本项目实测验证（2026-08），修改 `ds_client.py` 前务必先读本文。

## 关键坑（都踩过）

### 1. 默认是推理模型，reasoning 计入 max_tokens
- 不关推理时，`completion_tokens` 包含 reasoning tokens。
- 实测 `max_tokens=10` 返回**空 content**（预算全被 reasoning 吃掉），不报错。
- **关闭推理**：`extra_body={"thinking": {"type": "disabled"}}`，实测生效
  （`reasoning_tokens=None`，直接出答案）。
- `enable_thinking=False`、`reasoning={"type":"disabled"}` 均无效（被静默忽略）。
- 开推理的调用（如深读问答）`max_tokens` 必须给足（本项目用 8192）。

### 2. 空内容概率性出现
即使不用 JSON Output 也可能偶发空 content。`ds_client.chat()` 已内置重试
（指数退避 + 抖动），不要绕过它直接调 OpenAI SDK。

### 3. JSON Output
- `response_format={"type": "json_object"}`，system/user prompt 中必须含 "json"
  字样并给出格式示例（本项目所有 system prompt 已满足）。
- 与关闭推理可正常共用（实测）。
- 解析仍可能带围栏/尾逗号/None，用 `ds_client.parse_json_lenient()`。

### 4. 图片限制
| 项 | 值 |
|---|---|
| 单图 token | **封顶 384**（自动缩放到约 800×800） |
| 单请求图片数 | 600 张 |
| 15+ 图/请求时 | 单边最长 **4096px**（故渲染钳制在 3600px） |
| base64 内联 | 计入 48MiB 请求体，单图 ≤32MiB |
| Files API file_id | 单图 ≤64MiB，请求总限 200MiB |

### 5. Files API
- 上传：`client.files.create(file=f, purpose="user_data")` → `file-api-...`
- 引用：content 块 `{"type": "file", "file_id": "..."}`，可与 text 块交错排列
  （本项目用 `[第N页]` 文本标签 + file 块交错，实测有效，页码不乱）。
- openai SDK 的 `files.create()` **不接受 `expires_after`**（会 TypeError），
  文件默认永久有效；配额 25GiB / 10000 个文件，足够。
- `files.retrieve(fid)` 可校验存在性（免费、不耗 token）——`read_vision` / `ingest` 用它做失效重传。

## 设计决策（为什么这样做）

- **批量单请求**（25 页/请求）而非逐页调用：单图封顶 384 token，25 页 ≈ 1 万
  image token，加上 100 万上下文，远在配额内；比 PageIndex 的上百次小调用快一个
  数量级、便宜且稳。
- **thinking 双模式**：批量结构化抽取关推理（快/省/稳），最终深度回答开推理（质量）。
- **页码来自渲染**：每页图像由物理页码渲染而来并打上 `[第N页]` 标签，天然精确，
  不需要 PageIndex 那套"LLM 猜页码 → 验证 → 修复"循环。
