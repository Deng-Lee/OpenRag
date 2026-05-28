# E:\外汇文件 噪声分析与离线清洗方案

## 1. 目标与范围

本文档用于沉淀 `E:\外汇文件` 的离线噪声分析与清洗方案。第一阶段只产出可审计的 clean corpus，不覆盖原始文件，不修改 OpenRag 入库链路，也不做代码层面的实现。

参考方法来自 RAG 数据工程实践中的“多轮抽样 + LLM 初筛 + 人工审核 + 正则规则迭代 + 抽样复验”。该方法的核心不是一次性写完规则，而是先通过样本看清噪声形态，再逐轮收敛清洗边界。

第一版只处理现有 153 个 Markdown 文件。暂不重新解析 `.doc`、`.docx`、`.pdf`、`.xls`、`.xlsx` 文件，避免在首轮同时引入解析器差异、版式还原和 OCR 质量等额外变量。

离线清洗工具统一放在 `tools/clean/` 下：`tools/clean/scripts/` 存放脚本，`tools/clean/<流程阶段>/` 存放对应阶段产物。脚本和产物不写入源目录 `E:\外汇文件`。

## 2. 数据现状

本地目录 `E:\外汇文件` 当前共有 392 个文件，无子目录。类型分布如下：

| 类型 | 数量 | 占比 |
| --- | ---: | ---: |
| `.md` | 153 | 39.03% |
| `.docx` | 116 | 29.59% |
| `.doc` | 59 | 15.05% |
| `.pdf` | 56 | 14.29% |
| `.xls` | 6 | 1.53% |
| `.xlsx` | 2 | 0.51% |

首轮抽查发现：

- 153 个 `.md` 在按 UTF-8 显式读取时内容正常；此前看到的 `鍏充簬`、`骞` 等乱码来自 PowerShell 默认编码误读，不代表源文件真实损坏。
- manifest 中 `is_mojibake_suspected` 当前统计为 0；后续脚本必须显式按 UTF-8 读取 Markdown，避免再次把正常文件误判为乱码。
- Markdown 文件中普遍存在网页抓取残留，例如 `Source:` 行和 `javascript:void(0)` 附件链接。
- 文件名中存在 `_1`、`_2` 等重复版本，部分文件可能是同一文档的重复下载或不同格式副本。
- 部分文件属于申请表、登记表、应急申请单等表单类材料，应与制度正文、操作指南类文档分开评估。

## 3. 清洗流程

第一阶段采用离线、可回溯、保守清洗流程：

1. 建立 manifest：记录文件名、扩展名、大小、原始路径、是否 Markdown、是否疑似乱码、是否含网页残留、是否疑似重复版本、是否疑似表单类文件。
2. 编码确认与读取规范：对 `.md` 显式按 UTF-8 读取；只有 manifest 标记为真实疑似乱码时，才进入兜底修复分支，并保留原文与修复后文本的关联。
3. 分层抽样：按短文档、中等文档、长文档分层抽样，再额外抽取含附件链接、高重复标题、表单类关键词的文档。
4. 噪声标注：先由 LLM 辅助识别噪声模式，再人工确认哪些应删除、哪些应保留为正文或元数据。
5. 规则清洗：将确认后的噪声模式整理为行级规则，首轮只做保守删除或转元数据。
6. 人工复验：每轮规则调整后重新抽样检查，重点看误删正文和漏留噪声。
7. 输出 clean Markdown：将通过复验的文档写入独立输出目录，疑似空壳页、过短页、表单页进入待复核集合。

### 3.1 编码确认与读取规范

编码处理的主路径是“确认并固定读取方式”，不是默认修复。首轮 manifest 已显示 153 个 Markdown 的 `is_mojibake_suspected=false`，因此当前不需要对 Markdown 执行批量乱码修复。

此前出现的 `2026骞磋鐢熷搧浜ゆ槗鍐查攢鏃ュ巻` 这类文本，是正常 UTF-8 文件被 PowerShell 默认编码误读后的显示结果。同一文件按 UTF-8 显式读取时为 `2026年衍生品交易冲销日历`。因此，后续所有离线脚本必须显式指定 UTF-8 读取 Markdown，PowerShell 查看文件时也应使用 `-Encoding UTF8`。

建议采用 Python 3 标准库完成后续离线文本处理，PowerShell 只用于启动脚本和查看结果。原因是 Python 对路径遍历、JSONL、哈希、编码异常处理和跨平台复现更稳定；不需要引入第三方依赖，也不需要修改 OpenRag 运行时代码。

建议脚本职责如下：

| 脚本职责 | 说明 |
| --- | --- |
| 读取 manifest | 只处理已标记为 Markdown 的记录 |
| 显式 UTF-8 读取 | 统一使用 UTF-8 读取源 Markdown，禁止依赖 PowerShell 或系统默认编码 |
| 计算编码质量指标 | 记录乱码标记数量、中文字符比例、替换字符数量和文本长度 |
| 兜底修复判断 | 仅当 `is_mojibake_suspected=true` 或 UTF-8 质量异常时，才尝试候选修复 |
| 写入报告 | 每个 Markdown 文件记录读取状态、hash、质量指标、是否修复和失败原因 |

建议的临时脚本名称为 `normalize_foreign_exchange_md_encoding.py`。如果只是一次性离线处理，可放在临时目录运行，不纳入项目代码；如果需要项目内可复现留痕，再放到 `scripts/eval/`，但仍应保持为离线工具，不接入 OpenRag 入库链路。

编码读取校验脚本为 `tools/clean/scripts/check_foreign_exchange_md_encoding.py`。编码确认输出放在 `tools/clean/encoding_check/`，并生成：

| 输出物 | 用途 |
| --- | --- |
| `encoding_report.jsonl` | 每个 Markdown 文件一行，记录读取状态、hash、质量指标、是否修复和失败原因 |
| `encoding_summary.md` | 汇总 UTF-8 正常数量、修复数量、失败数量、待人工复核数量和代表样例 |

质量判定建议使用保守阈值：

- UTF-8 读取后典型乱码标记数量应接近 0，例如 `鍏`、`涓`、`骞`、`佸` 等不应大面积出现。
- UTF-8 读取后中文字符比例应符合中文金融文档特征，且不应出现大量 `�` 替换字符。
- 重新写出前后非空文本长度不应变化；长度异常变化的文件进入 `needs_review`。
- 只有真实疑似乱码文件才尝试 `cp936 -> utf-8` 候选修复；如果路径抛出编码异常，或修复后质量分没有提升，不输出到 clean 集合，只记录为待复核。
- `Source:` 行、附件链接和其他网页噪声在此步骤保留原样，留给后续“规则清洗”阶段处理。

该步骤的验收标准：

- manifest 中 153 个 Markdown 均有一条 `encoding_report.jsonl` 记录。
- 当前 manifest 统计下，修复数量应为 0，UTF-8 正常数量应为 153；如果后续新增真实疑似乱码文件，再记录修复数量。
- 抽样查看至少 10 个源 Markdown，使用 UTF-8 显式读取时标题、正文、日期、机构名和附件名均可正常阅读。
- 原始 `E:\外汇文件` 文件不发生任何写入或覆盖。

### 3.2 分层抽样方案

分层抽样脚本为 `tools/clean/scripts/stratified_sample_markdown.py`。脚本读取 `tools/clean/manifest/manifest.jsonl` 和 `tools/clean/encoding_check/encoding_report.jsonl`，只抽取 Markdown 文件，并将首轮人工审阅样本输出到 `tools/clean/samples/`。

首轮默认参数：

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `sample_size` | 40 | 首轮人工审阅样本数 |
| `seed` | 20260528 | 固定随机种子，保证抽样可复现 |
| `short` | 16 | `char_count <= 800` 的基础抽样数 |
| `medium` | 10 | `801 <= char_count <= 3000` 的基础抽样数 |
| `long` | 全量 | `3001 <= char_count <= 8000` 的长文档全覆盖 |
| `very_long` | 全量 | `char_count > 8000` 的超长文档全覆盖 |

基础长度层抽样完成后，再按风险层补样：`javascript:void(0)` 附件链接、疑似重复版本、疑似表单类、短文档且含附件链接的疑似空壳页。最终去重后控制在 40 篇，优先保留长文档、超长文档、表单类和重复版本样本。

当前首轮执行结果：

- 候选 Markdown：153。
- 实际样本数：40。
- 长度层覆盖：短文档 25、中等文档 10、长文档 3、超长文档 2。
- 风险层覆盖：附件链接 34、重复版本 8、表单类 8、疑似空壳页 22。
- 输出文件：`round1_sample_manifest.jsonl`、`round1_review.md`、`round1_sample_summary.md`。

人工审阅时重点判断：

- `Source:` 是否只转元数据，不进入正文。
- `javascript:void(0)` 附件行是删除、保留附件名，还是转附件元数据。
- 是否属于“仅提示下载附件”的空壳页。
- 重复版本中应保留哪一份。
- 表单类文件是否进入 RAG 正文库，还是单独分流。
- 是否发现新的噪声模式，供下一步“噪声标注/规则清洗”使用。

### 3.3 规则清洗执行

规则清洗脚本为 `tools/clean/scripts/clean_foreign_exchange_md.py`。脚本读取 `tools/clean/manifest/manifest.jsonl` 和 `tools/clean/encoding_check/encoding_report.jsonl`，只处理 UTF-8 校验通过的 Markdown 文件，并将清洗结果写入 `tools/clean/clean_md/`，将每篇文件的处理决策写入 `tools/clean/noise_report/cleaning_decisions.jsonl`。

本轮采用人工审阅后确认的规则作为主规则：

- `Source:` 行直接删除，不进入正文，也不在本轮转为正文元数据。
- `javascript:void(0)` 附件链接行直接删除，不保留附件名，也不转附件元数据。
- 附件提示行、附件列表引导行、下载后阅读提示行直接删除。
- 删除上述噪声后只剩标题或极短下载提示的文档，判定为 `dropped_download_only_page`。
- 删除上述噪声后只剩简短“发布/印发/修订某附件文件”的通知，且不包含规则条款、操作步骤、业务安排等实质正文的文档，判定为 `dropped_brief_publish_notice`。
- 清洗后正文完全相同的文档，只保留按文件名排序最先出现的一篇，其余判定为 `dropped_exact_duplicate`。

当前执行结果如下：

| 决策 | 数量 |
| --- | ---: |
| `kept` | 85 |
| `dropped_download_only_page` | 31 |
| `dropped_brief_publish_notice` | 37 |

本轮尚未出现清洗后完全一致的重复正文，因此 `dropped_exact_duplicate` 当前为 0。脚本仍保留该规则，后续新增文件或规则调整后如出现完全重复正文，会自动稳定去重。

### 3.4 clean_md 抽样复验

clean Markdown 复验脚本为 `tools/clean/scripts/review_clean_md_quality.py`。脚本从 `tools/clean/clean_md/` 中按固定随机种子抽样，结合 `tools/clean/noise_report/cleaning_decisions.jsonl` 检查两类风险：

- 漏删风险：`Source:`、`javascript:void(0)`、附件提示、下载提示、重复标题是否仍残留在 clean 正文中。
- 误删风险：clean 正文是否过短、是否删除了大量邻近正文的噪声行，供人工抽看判断。

首轮复验参数与产物：

| 项目 | 值 |
| --- | --- |
| 样本数 | 30 |
| 随机种子 | 20260528 |
| 复验明细 | `tools/clean/quality_check/round1_clean_md_review.jsonl` |
| 复验报告 | `tools/clean/quality_check/round1_clean_md_review.md` |

首轮复验发现一类漏删：部分文档删除 `附件：` 和附件链接后，仍留下独立编号的附件标题，如 `1.中央债券借贷业务服务协议`。该问题已补充为“附件提示块”规则：遇到 `附件：` 后，继续删除紧随其后的独立编号附件标题，直到遇到非附件标题正文。重跑后，样本和全量 clean Markdown 中均未发现 `Source:`、`javascript:void(0)`、附件冒号提示或下载提示残留。

## 4. 初始噪声规则

首版规则只处理高确定性噪声，不删除可能承载业务含义的金融制度正文。

建议优先处理以下噪声类型：

| 噪声类型 | 处理方式 | 说明 |
| --- | --- | --- |
| `Source:` 行 | 从正文删除 | 本轮不转元数据，后续如需要可从原始文件或 manifest 回溯 |
| `javascript:void(0)` 链接 | 删除整行 | 常见于网页附件列表，不保留附件名 |
| 附件下载提示 | 删除 | 如“点击下载后阅读”等页面操作提示 |
| 附件提示块中的编号附件标题 | 删除 | 如 `附件：1. xxx` 后续独立成行的 `2. xxx` |
| 纯页码或纯数字行 | 删除 | 避免污染检索，但需谨慎保留法规编号上下文 |
| 分隔线 | 删除 | 如连续横线、无语义装饰线 |
| 重复标题 | 合并或删除重复项 | 保留首个标题，避免标题重复入块 |
| 空壳页 | 直接删除并记录决策 | 如正文只剩标题、附件或下载提示 |
| 简短附件指向型公告 | 直接删除并记录决策 | 如仅公告发布某附件文件，但没有正文条款或操作内容 |
| 完全重复正文 | 保留一篇，其余删除并记录保留对象 | 以清洗后正文 hash 判断 |
| 表单类文件 | 标记分流，不在本轮自动删除 | 申请表、登记表可单独决定是否入库 |

需要明确保留的内容：

- 监管文件编号、公告编号、规则条款编号。
- 日期、交易品种、市场名称、机构名称等业务关键字段。
- 表格内容和表格标题。
- 中英文混排标题、产品名、协议名、系统名。

## 5. 输出物

首轮离线清洗工具和产物统一放在 `tools/clean/`。建议包含：

| 输出物 | 用途 |
| --- | --- |
| `tools/clean/scripts/*.py` | 离线清洗脚本，不接入 OpenRag 入库链路 |
| `tools/clean/manifest/manifest.jsonl` | 每个源文件一行，记录文件属性、检测标签、处理状态 |
| `tools/clean/manifest/manifest_summary.md` | manifest 阶段统计汇总 |
| `tools/clean/encoding_check/encoding_report.jsonl` | Markdown 编码读取校验明细 |
| `tools/clean/encoding_check/encoding_summary.md` | Markdown 编码读取校验汇总 |
| `tools/clean/samples/round1_review.md` | 首轮抽样审阅包，便于人工确认噪声规则 |
| `tools/clean/noise_report/cleaning_decisions.jsonl` | 每个 Markdown 的清洗决策、命中规则、删除行数和 clean hash |
| `tools/clean/clean_md/*.md` | 编码确认和规则清洗后的 Markdown 正文 |
| `tools/clean/noise_report/noise_report.md` | 规则命中统计、典型样例、疑似误删风险 |
| `tools/clean/quality_check/round1_clean_md_review.*` | clean Markdown 首轮抽样复验明细和报告 |
| `tools/clean/needs_review/*.md` | 空壳页、过短页、表单页或清洗不确定的文档 |

输出目录不得覆盖 `E:\外汇文件` 下的原始文件。每个 clean 文件应能追溯到原文件路径和规则版本。

## 6. 质检标准

首轮验收以“可读、可审计、低误杀”为准：

- 编码读取一致：153 个 `.md` 均按 UTF-8 显式读取，manifest 当前 `is_mojibake_suspected=0` 的结论应保持一致；不得再用系统默认编码误判乱码。
- 规则命中报告：每条规则输出命中文件数、命中行数和代表性样例。
- 抽样复验：从清洗后文档随机抽取 30 篇，人工检查是否误删正文、是否漏留明显噪声。
- 过短分流：清洗后正文少于 100 字、只剩附件链接、或疑似空壳页的文件进入 `needs_review`。
- 可回溯：每个 clean 文件记录原始文件名、源 URL、清洗规则版本、删除行摘要。
- 保守删除：任何可能影响法规条款、操作步骤、产品说明、市场规则理解的内容，默认保留或进入人工复核。

## 7. 后续衔接

首轮离线清洗完成后，可以根据质检结果选择后续方向：

- 如果 Markdown 清洗效果稳定，再扩展到 `.doc`、`.docx`、`.pdf`、`.xls`、`.xlsx` 的重新解析和清洗。
- 如果规则误杀率低且噪声模式稳定，再考虑接入 OpenRag 入库前处理。
- 如果要用于检索质量评估，可基于 clean Markdown 生成 mini eval 语料，先人工审核 50 条问题和证据片段，再执行检索评估。
- 对重复版本文件，可在后续阶段基于文件名规范化、大小、文本 hash 和正文相似度建立去重策略。

## 假设

- 本文档只描述流程、规则和验收标准，不包含可执行脚本或代码片段。
- 现有 `docs/eval/foreign_exchange_file_word_summary.md` 和统计 CSV 保持不变。
- 第一阶段以 153 个 Markdown 文件为主，不处理非 Markdown 文件的重新解析。
- 清洗脚本和输出全部写入 `tools/clean/`，不覆盖原始文件。
