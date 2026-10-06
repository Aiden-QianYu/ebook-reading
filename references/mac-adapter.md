# Mac 阅读适配与执行

## 读取能力

`books_library.py` 使用 SQLite `mode=ro`、`query_only` 读取 Books 内部书库。当前支持 `ZBKLIBRARYASSET` 与 `ZAEANNOTATION` 的已观察 schema；它们不是 Apple 公共 API，更新后要重新验证。快照可能落后于阅读窗口或其他设备，当前实际界面优先。不使用 `immutable=1` 忽略 WAL，不改数据库，不读账号凭据。

没有可依赖的 Books AppleScript 阅读接口。UI 使用当前 Computer Use 工具并先读取文档/应用状态，不用 osascript/System Events 绕过权限。本地 EPUB 可读时不依赖 UI；缺少 UI 权限只影响当前位置核对及视觉/保护内容的界面读取。

下文 `<skill>` 是实际技能目录，`work/` 是任务临时目录。动态路径、书名、锚点优先通过 Python `subprocess.run([...])` 参数数组传入，不用 JSON 字符串当 shell 转义。

## 1. 获取书籍及真实目录

```text
python3 <skill>/scripts/books_library.py --asset-id <选定ID> --output work/book.json
python3 <skill>/scripts/epub_session.py inspect --book-json work/book.json --output work/outline.json
```

`inspect` 返回 spine 与导航目录 `outline.entries`；每项有 `outline_id`、标题、层级、定位和可能的解析警告。选择真正的章节节点，排除分部容器与章内小节。优先 EPUB 3 nav，兼容 EPUB 2 NCX；目录缺失时 headings 只是待核对候选，不自动认作章。

在 `work/chapter-selection.json` 写出按正文顺序的真实章节节点：

```json
{"chapter_outline_ids": ["toc-2", "toc-5", "toc-8"]}
```

IDs 是示意，实际取 inspect 结果。索引应覆盖整本书的阅读章节；时间模式从前言开始时也要纳入经核对的相应目录节，否则范围校验会失败。章节号从真实标题识别，未编号节使用目录顺序并明确标注未编号。

```text
python3 <skill>/scripts/epub_session.py index --book-json work/book.json --chapter-selection work/chapter-selection.json --output work/chapters.json
```

索引按目录链接及文内 fragment 定位：同一文件的多个章节会使用不同正文偏移；跨文件章节覆盖完整区间。每章结束在下一同级/上级目录项或下一选定章的起点，末章有附录/致谢时在该目录项前结束。目录存在分部但没有链接时，务必检查相邻边界，必要时用原书正文锚点人工纠正索引。修正必须来自实读正文，不能估计。

## 2. 按明确章节读取

```text
python3 <skill>/scripts/epub_session.py slice --book-json work/book.json --chapter-map work/chapters.json --chapters 3-5 --output work/reading.json
```

`--chapters 3,5` 只读这两章，范围不会包含 4。此模式从完整章节起点读到章末，不使用默认分钟数。若已有 AI 状态，把现有 `session_id` 用 `--previous-session` 传入，作为保存并发检查；它不决定绝对章节起点。

章节号有重复（如多卷）或目录层级难以确定时，先区分卷/标题再生成唯一索引，不自动选一个版本。编号明确的章按其原章号；未编号节按索引 ordinal，调用前说明编号含义。

## 3. 相对续读与时间模式

已有 AI 游标时：

```text
python3 <skill>/scripts/epub_session.py slice --book-json work/book.json --chapter-map work/chapters.json --resume-state <摘要目录>/.reading-state/<asset_id>.json --next-chapters 2 --output work/reading.json
```

未读完的当前章计作第一章，不跳掉其余内容。章末边界后从下一章开始；只有一句空白尾巴时要依据真实正文核对，不把空白当一章。章节不足时返回实际余下章数。

没有章节要求时删去 `--next-chapters`，使用 `--minutes 30 --zh-rate 400 --en-rate 200`。摘要会注明终止章未完和精确末尾原文。明确要求从下一章读完整两章时，先将起点设为下一章目录定位，再相对读取。

首次 Books 位置候选：

```text
python3 <skill>/scripts/epub_session.py locate --book-json work/book.json --cfi <候选CFI>
```

核对附近文字与 UI/用户当前页面后，slice 使用 `--cfi <已核对CFI> --verified-position`，也可使用唯一 `--anchor <当前页面原句>`。绝对章节无需此起点。`--from-start` 仅用于用户要求从书头开始；`--member` 是文件起点，不能冒充章节；`--through-member` 只是抽取工具的文件上界。

type 3 annotation 只保留“内部 schema 的未核对位置候选”标记。CFI 支持常见文本点与范围起点，UTF-16 偏移，ID assertion 匹配；Books itemref 缺少 id 时允许同节点 idref 匹配。复杂 CFI 扩展明确失败，改用原文锚点。文件指纹包含 OPF、线性 XHTML 和 nav/NCX；图片/CSS 变化不一定改变指纹。

## 4. 摘要并保存

阅读 reading.json 的全部 segments，写出主体 `work/note-body.md`，准备 `work/quotes.json`。body 只保留一次 `<!-- VERIFIED_QUOTES -->`，不自行写范围前言、进度/YAML/技术记录。保存脚本根据真实片段生成范围与最后一句。

```text
python3 <skill>/scripts/save_session.py --config <skill>/config.json --reading work/reading.json --body work/note-body.md --quotes work/quotes.json
```

保存器合并同目录 `config.local.json`，校验精句，独占创建完整文件，再更新 AI 游标和去重 receipt。文件命名为时间-书名-起始章名；重复保存返回原文件，旧阅读段重试不回退当前游标。并发/来源冲突时重新读取状态，不强制覆盖。

## PDF、UI 与降级

PDF/正常界面片段需有同样的真实信息：`format`（pdf/ui）、asset_id、title、author、source_sha256、source_quality、created_at、planned_minutes（章节模式为 null）、estimated_minutes、books_progress_snapshot、scope_mode、reading_range、starting_chapter_title、segments、start_locator、end_locator。segments 的 member 可为 `pdf-page-17`，偏移是对应工具提取文本的字符位置。最后一句由实际末尾 text 计算；quotes 同样匹配。

存在可靠续读定位时加入 `next_cursor`；否则省略，下次用开头末尾原文重新核对。不得伪造 EPUB 游标。区分 PDF 页序和印刷页码；图像/表格/公式核对原页。正文缺失、DRM 不可正常读取或无视觉工具时说明限制，请求合法片段，不绕过保护。

没有目标写权限时先完成工作目录笔记，再申请精确目录权限。Obsidian 打开链接使用其 URI，生成链接不代表已在应用中显示；无需社区插件/HTTP 服务。

参考：[Apple Books 阅读](https://support.apple.com/guide/books/read-books-ibks5f526382/mac)、[W3C EPUB 导航目录](https://www.w3.org/TR/epub-33/#sec-nav-toc)、[EPUB CFI](https://w3c.github.io/epub-specs/epub33/epubcfi/)、[Obsidian URI](https://help.obsidian.md/uri)。
