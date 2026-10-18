# PYeyedata input templates / PYeyedata 输入模板说明

资源集版本：`c03-resources/v3`；模板 schema 版本：`contract-1.3.0`；代码合同版本：`1.3.0`。字段来源与文件哈希见本目录的 `template-manifest.json`，各类资源入口见上级 `resource-index.json`。signals、segments、performance 和 AOI 的 XLSX 均有 `empty_template`、`example`、`fields` 三张工作表；正常导入时必须在界面明确选择前两者之一，`fields` 只供查阅，不能作为数据表导入。CSV 空模板与示例 CSV 是独立文件。AOI workbook 已提供矩形和多边形示例。所有示例行均明确为 `SYNTHETIC_ONLY`，实际研究前必须替换或删除。模板导入使用普通 `read_table(role=...)` 和公开 normalizer，不存在绕开普通导入的专用路径。

Resource set: `c03-resources/v3`; template schema: `contract-1.3.0`; code contract: `1.3.0`. File hashes and canonical schema references are in `template-manifest.json`, with role-specific entry points in the parent `resource-index.json`. The signals, segments, performance, and AOI XLSX workbooks each have `empty_template`, `example`, and `fields` sheets. In the normal import UI, explicitly choose one of the first two; `fields` is documentation and must never be imported as data. CSV empty templates and examples are separate files. The AOI workbook includes rectangle and polygon examples. Every example row is labeled `SYNTHETIC_ONLY` and must be replaced or removed before real work. Templates use the normal `read_table(role=...)` and public normalizers; there is no template-only import path.

## 通用导入原则 / General import rules

- 明确选择时间单位、坐标单位和坐标系；列名里的 `_s` 是模板字段约定，不会代替导入对话框中的确认。若源文件使用毫秒、帧号或日期时间，请保留源值并在正常导入选项中选择实际单位、原点、时区或帧率。禁止只凭数值大小推断单位。
- Confirm time unit, coordinate unit, and coordinate frame explicitly in the ordinary import dialog. The `_s` suffix documents this template; it does not replace a user-confirmed import choice. For milliseconds, frame indices, or datetimes, preserve the raw value and supply the real unit, origin, timezone, or frame rate. Never infer units from magnitude alone.
- 主体、会话、互动、条件、试次、刺激和时钟标识必须来自真实设计或经用户明确分配；不得根据文件顺序或样本行自动推断。共享互动只有在明确同步时钟映射后才可选择 `shared_interaction`。
- Subject, session, interaction, condition, trial, stimulus, and clock IDs must come from the study design or an explicit user assignment; they are never inferred from file order. Use `shared_interaction` only after explicitly declaring a synchronized clock mapping.
- 缺失/无效眼动样本必须如实编码；`1/true/yes/valid` 与 `0/false/no/invalid` 是当前内置有效性代码。厂商自定义码须在导入时显式映射。不要把无效编码改成有效样本，也不要补造峰值、眨眼或事件。
- Retain missing/invalid eye samples as recorded. Built-in validity labels are `1/true/yes/valid` and `0/false/no/invalid`; map vendor-specific labels explicitly during import. Do not convert invalid samples to valid or invent peaks, blinks, or events.
- 合成示例不含参与者信息，也不代表任何设备验证。模板中的 ID 使用 `SYNTHETIC_ONLY` 前缀；合成数据不能解除软件中的科学、同步或设备门槛。
- Synthetic rows contain no participant information and establish no device validation. Their IDs use the `SYNTHETIC_ONLY` prefix; synthetic data cannot bypass scientific, synchronization, or device gates.

## `signals-contract-1.3.0` — 眼动信号 / Signal samples

空表文件：`signals-contract-1.3.0.csv`（header only）或 `signals-contract-1.3.0.xlsx` 的 `empty_template` 工作表。合成示例文件：`signals-example-contract-1.3.0.csv` 或同一XLSX的 `example` 工作表。XLSX的 `fields` 工作表列出对应字段类型和单位。

Empty files: `signals-contract-1.3.0.csv` (header only) or the `empty_template` sheet in `signals-contract-1.3.0.xlsx`. Synthetic examples: `signals-example-contract-1.3.0.csv` or the `example` sheet in the same workbook. The workbook's `fields` sheet lists field types and units.

| 列 / Field | 类型与单位 / Type and unit | 导入映射与说明 / Mapping and use |
|---|---|---|
| `subject_id` | 文本 / text | 主体 ID；必须显式填写 / Explicit subject ID. |
| `session_id` | 文本 / text | 会话/录制 ID；必须显式填写 / Explicit session or recording ID. |
| `time_s` | 有限数值 / finite number, seconds | 规范时间列；确认源值单位、原点、时钟和精度。`time_raw` 会保留导入源值，`original_index` 由行顺序记录 / Canonical time field; confirm source unit, origin, clock, and precision. The importer retains `time_raw` and records row order as `original_index`. |
| `gaze_x`, `gaze_y` | 有限数值 / finite number, coordinate unit | 注视点 X/Y；同一文件保持同一坐标单位和 frame，且在导入元数据中声明 / Gaze point X/Y; use one coordinate unit/frame and declare both in import metadata. |
| `gaze_valid` | 布尔代码 / boolean code | 该源点有效性；默认接受 `1/0`、`true/false`、`yes/no`、`valid/invalid` / Validity of the source point; built-in labels are listed above. |
| `gaze_left_valid`, `gaze_right_valid` | 布尔代码 / boolean code | 双眼各自有效性；缺失应留空，不等于有效 / Per-eye validity; blank means unknown/missing, not valid. |
| `pupil_left`, `pupil_right` | 有限数值 / finite number, source unit | 原始双眼瞳孔列；记录原始单位，若单位不明请在元数据中标为 unknown / Raw per-eye pupil channels; record source units or declare unknown. |
| `stimulus_id` | 文本 ID / text ID | 将样本映射到明确的刺激定义 / Links samples to an explicit stimulus definition. |
| `condition` | 文本类别 / text category | 研究条件 / Study condition. |
| `trial` | 整数或文本 ID / integer or text ID | 试次标识；保留真实重复测量结构 / Trial identity; preserve repeated-measures structure. |

导入角色为 `signals`。模板不要求输入 `original_index` 或 `time_raw`：导入器保留原时间值并生成源行索引。不要在数据列填入 `device_verified`、认证状态或未经证实的厂商/设备质量结论。坐标可以是像素点、角度或其他明确的表示，但需在实际分析前给出匹配的单位、坐标 frame 与必要几何信息。

Import role: `signals`. The source template does not require `original_index` or `time_raw`: the importer retains raw time and creates the source-row index. Do not put `device_verified`, certification claims, or unverified device-quality conclusions into data columns. Coordinates may be pixels, degrees, or another explicit representation, but analysis requires matching units, coordinate frame, and any necessary geometry.

## `segments-contract-1.3.0` — 区间标注 / Segment marks

空表文件：`segments-contract-1.3.0.csv`（header only）或 `segments-contract-1.3.0.xlsx` 的 `empty_template` 工作表。合成示例文件：`segments-example-contract-1.3.0.csv` 或同一XLSX的 `example` 工作表。XLSX的 `fields` 工作表列出对应字段类型和单位。

Empty files: `segments-contract-1.3.0.csv` (header only) or the `empty_template` sheet in `segments-contract-1.3.0.xlsx`. Synthetic examples: `segments-example-contract-1.3.0.csv` or the `example` sheet in the same workbook. The workbook's `fields` sheet lists field types and units.

| 列 / Field | 类型与单位 / Type and unit | 导入映射与说明 / Mapping and use |
|---|---|---|
| `subject_id`, `session_id` | 文本 ID / text ID | participant/session 模式每行必须显式匹配源信号；共享互动行可由已声明的成员会话映射补全 / Required per row in participant/session mode; shared-interaction mode needs an explicit member-session map. |
| `segment_id` | 文本 ID / text ID | 可由研究者提供；不得用行号代替有意义的真实身份 / Optional explicit ID; do not treat row number as study identity. |
| `segment_name` | 文本 / text | 必需的人工标签；普通导入不猜测标签 / Required human-readable label; never inferred by the importer. |
| `start_s`, `end_s` | 有限数值 / finite number, seconds | 左闭右开 `[start, end)` 边界；必须 `end_s > start_s` / Left-closed, right-open interval; `end_s` must exceed `start_s`. |
| `condition`, `trial`, `stimulus_id` | 文本/整数 ID / text or integer ID | 可选的真实设计标签；无依据时留空 / Optional study labels; leave blank if unknown. |

导入角色为 `segments`。边界时间单位需与信号一致并在导入选项中确认。模板示例使用 participant/session scope。若选择 `shared_interaction`，必须另外提供明确互动 ID、同步时钟声明及成员—会话映射；这些元数据不能从示例行推断。

Import role: `segments`. Boundary units must match signal time and be confirmed in import options. The sample uses participant/session scope. `shared_interaction` additionally requires explicit interaction identity, synchronized-clock declaration, and member-to-session mapping; these are never inferred from the sample row.

## `aois-contract-1.3.0` — ROI/AOI 几何 / AOI geometry

CSV的`aois-contract-1.3.0.csv`是header-only空模板；`aois-example-contract-1.3.0.csv`是两行合成示例。XLSX同样分为`empty_template`、`example`和`fields`三张工作表。导入XLSX时必须明确选择前两者之一；`fields`只供查阅，不是数据表。

The CSV `aois-contract-1.3.0.csv` is a header-only template; `aois-example-contract-1.3.0.csv` contains two synthetic rows. The workbook has `empty_template`, `example`, and `fields` sheets. Select either of the first two sheets explicitly when importing; `fields` is documentation, not data.

AOI域schema版本为`2.0.0`，静态几何方法版本为`static-aoi-geometry-2.0.0`。这两个版本与全局导入合同`1.3.0`分开记录，不改变其他三类模板的合同。

The AOI domain schema is `2.0.0`; the static geometry method is `static-aoi-geometry-2.0.0`. These versions are recorded separately from global import contract `1.3.0` and do not change the other three template contracts.

| 列 / Field | 类型与单位 / Type and unit | 导入映射与说明 / Mapping and use |
|---|---|---|
| `aoi_id` | 文本 ID / text ID | 必填稳定身份，用于连接和结果；不可用名称代替 / Required stable identity for joins and results; never replace it with a name. |
| `stimulus_id` | 文本 ID / text ID | 必填；与刺激定义精确匹配 / Required exact match to a stimulus definition. |
| `shape` | `rectangle` 或 `polygon` / enum | 必填；新表只写这两个规范值 / Required; new tables use these canonical values. |
| `aoi_name` | 非空文本 / nonblank text | 新普通表必填；保留来源文本和中文，不替代`aoi_id` / Required in new normal files; preserve source text and Chinese; does not replace `aoi_id`. |
| `xmin`, `ymin`, `xmax`, `ymax` | 有限二进制64位数值 / finite binary64 | 矩形必填；须`xmax > xmin`且`ymax > ymin`。负坐标允许 / Required for rectangles; require `xmax > xmin` and `ymax > ymin`. Negative coordinates are allowed. |
| `vertices` | JSON文本，Nx2数值数组 / JSON text, Nx2 numeric array | 多边形必填，格式如`[[0,0],[100,0],[100,100],[0,100]]` / Required for polygons; for example `[[0,0],[100,0],[100,100],[0,100]]`. |
| `unit`, `coordinate_frame` | 文本 / text | 可选来源元数据；必须显式确认，不自动缩放或推断变换 / Optional source metadata; declare explicitly; no automatic scaling or inferred transform. |
| `overlap_policy` | `exclusive`或`multi_label` / enum | 可选；重叠策略须明确，不按来源行顺序决胜 / Optional; declare the overlap policy; source row order never breaks ties. |
| `priority` | 有限数值 / finite number | `exclusive`模式需要命中的AOI有唯一优先级；数值最低者胜出 / In `exclusive` mode, hit AOIs need unique priorities; the lowest value wins. |

多边形顶点必须为标准JSON有限Nx2数组：至少3个不同顶点，最多1024个（去除至多一个显式闭合点后）。允许隐式闭合，或恰好一个与首点相等的末尾闭合点；只移除该冗余末点并记录闭合方式。其他重复顶点、零长度边、自交、非相邻边相交/接触/重叠、相邻回折或零面积都无效；不会自动修复。凸形、凹形和两种绕序均可。JSON只接受数值，不接受布尔值、null、NaN、Infinity、字符串坐标、参差/多层数组或对象。

Polygon vertices must be a standard JSON finite Nx2 array with at least three distinct vertices and at most 1,024 after removing at most one explicit closing point. Implicit closure is accepted, as is exactly one trailing point equal to the first; only that redundant endpoint is removed and the input closure is audited. Other repeated vertices, zero-length edges, self-intersections, nonadjacent edge intersections/touches/overlaps, adjacent backtracking, and zero area are invalid; geometry is never repaired automatically. Convex and concave polygons and either winding are allowed. JSON coordinates must be numbers, not booleans, null, NaN, Infinity, strings, ragged/nested arrays, or objects.

边界采用`closed`语义：矩形和多边形的边及顶点都属于区域。按输入中实际表示的坐标精确比较，容差为0；不使用epsilon、吸附或缓冲区。这个规则不代表设备或物理定位精度。

Both rectangles and polygons use a `closed` boundary: points on an edge or vertex are inside. Membership uses exact comparisons on represented coordinates with zero tolerance; no epsilon, snapping, or buffer is used. This does not establish device or physical positioning accuracy.

多个AOI之间允许重叠，但一个多边形自身不能自交。`exclusive`要求所有命中AOI有唯一有限`priority`，选择最低数值；缺失、重复、混用或无法解析时，结果不可用并说明原因。`multi_label`保留全部命中稳定ID。不得用表格行顺序解决歧义。

Overlap between different AOIs is allowed, but an individual polygon cannot self-intersect. Under `exclusive`, every hit AOI must have a unique finite `priority`; the lowest value wins. Missing, duplicate, mixed, or unresolved priorities make the result unavailable with a reason. `multi_label` retains every hit stable ID. Table row order never resolves ambiguity.

AOI名称是人类可读元数据，`aoi_id`仍是连接、结果和审计身份。新schema表缺少或留空`aoi_name`时无效。旧typed表或旧归档可在显式兼容迁移时保留`legacy_missing`状态；显示层可用“未提供名称（ID: …）”作为提示，但不得伪称名称来自源数据。迁移必须创建新副本/修订并记录审计，不静默改写历史。

AOI names are human-readable metadata; `aoi_id` remains the identity for joins, results, and audit. A new-schema table with a missing or blank `aoi_name` is invalid. Old typed tables or archives may retain `legacy_missing` only through an explicit audited compatibility migration. A display may show “Name not supplied (ID: …)” as a fallback, but must not claim that name came from the source. Migration creates a new copy/revision and does not silently rewrite history.

方法输出的`aoi_name_status`、`aoi_domain_schema_version`、`aoi_geometry_version`、`aoi_boundary_policy`、`aoi_input_closure`、`aoi_geometry_fingerprint`、`aoi_source`、`aoi_geometry_raw`和`aoi_geometry_audit`是生成的审计/结果字段，不要把它们当成来源输入列填写。几何指纹不包含名称、来源路径或原始文本；原始几何和来源审计单独保留。

The method generates `aoi_name_status`, `aoi_domain_schema_version`, `aoi_geometry_version`, `aoi_boundary_policy`, `aoi_input_closure`, `aoi_geometry_fingerprint`, `aoi_source`, `aoi_geometry_raw`, and `aoi_geometry_audit` as output/audit fields. Do not enter them as source columns. The geometry fingerprint excludes names, source paths, and raw spelling; original geometry and source provenance are retained separately.

容量上限为：每个多边形JSON UTF-8文本262,144字节；每张表1024个AOI行、总顶点数16384；每个多边形最多1024个顶点；每表最多200万次拓扑边对测试、每个scope最多500万次成员边测试、每个任务最多20万次精确谓词计算。超限报告`aoi_geometry_resource_limit`并标记结果不可用；不得截断顶点/样本、近似、部分计数或把无效几何当成区域外。

Capacity limits are 262,144 UTF-8 bytes per polygon JSON value; 1,024 AOI rows and 16,384 total vertices per table; 1,024 vertices per polygon; 2,000,000 topology edge-pair tests per table; 5,000,000 membership edge tests per scope; and 200,000 exact predicates per task. Exceeding a limit reports `aoi_geometry_resource_limit` and makes the result unavailable. Do not truncate vertices/samples, approximate, partially count, or treat invalid geometry as outside.

资源所有者已对 AOI 示例运行公开 `read_table`、`normalize_aois` 和静态几何审计：示例规范化成功，审计记录 domain schema `2.0.0`、方法 `static-aoi-geometry-2.0.0`、闭边界及顶点数。这是资源示例检查，不是独立 C05 验收，也不证明真实设备或生理有效性。

The resource owner ran the public `read_table`, `normalize_aois`, and static geometry audit against the AOI example. Normalization succeeded; the audit records domain schema `2.0.0`, method `static-aoi-geometry-2.0.0`, closed boundaries, and vertex counts. This is a resource-example check, not independent C05 acceptance and not evidence of real-device or physiological validity.

## `performance-contract-1.3.0` — 参与者表现 / Participant performance

空表文件：`performance-contract-1.3.0.csv`（header only）或 `performance-contract-1.3.0.xlsx` 的 `empty_template` 工作表。合成示例文件：`performance-example-contract-1.3.0.csv` 或同一XLSX的 `example` 工作表。XLSX的 `fields` 工作表列出对应字段类型和单位。

Empty files: `performance-contract-1.3.0.csv` (header only) or the `empty_template` sheet in `performance-contract-1.3.0.xlsx`. Synthetic examples: `performance-example-contract-1.3.0.csv` or the `example` sheet in the same workbook. The workbook's `fields` sheet lists field types and units.

| 列 / Field | 类型与单位 / Type and unit | 导入映射与说明 / Mapping and use |
|---|---|---|
| `subject_id`, `session_id`, `condition`, `trial` | 文本/整数 ID / text or integer IDs | 可能的连接键；分析配置必须由用户显式选定全部连接键并满足一对一 / Candidate join keys; users must explicitly select complete keys and satisfy one-to-one matching. |
| `accuracy` | 二元 `0/1` / binary | 示例是二元表现，不表示任何结果已分析 / Binary example; it does not mean an analysis has been run. |
| `reaction_time_s` | 有限数值 / finite number, seconds | 连续反应时示例；原始单位须确认后再转换 / Continuous RT example; confirm source units before conversion. |

导入角色为 `performance`。该输入表没有固定的全局 required-field schema：实际分析所需的 performance 列和 join keys 由研究设计显式选择。不得按主体姓名猜连接键；键存在缺失或重复时，一对一连接被阻止。`accuracy` 为二元变量，不能当连续结果强行相关；`reaction_time_s` 只有在所选方法接受连续目标、键完整且设计条件满足时才能使用。

Import role: `performance`. This input has no single global required-field schema: the research design explicitly selects performance columns and join keys. Never guess join keys from names; missing or duplicate keys block one-to-one joins. `accuracy` is binary and must not be forced into continuous correlation; `reaction_time_s` is usable only when the selected method accepts a continuous target and the design/key checks pass.

## 当前范围 / Current scope

这些双语文件是模板字段补充说明。最终用户指南已保存为 `help/PYeyedata-user-guide-zh.pdf`，可在应用“结果与追溯”页点击“使用说明”离线打开。指南中的界面截图只帮助识别控件；若画面出现测试行，不代表分析证据，也不代表整套流程已验收。所有合成场景、数据值和参考表只用于软件路径检查；设备验证和生理有效性均为 `NOT_RUN`。

These bilingual files supplement the offline template field definitions. The user guide is available at `help/PYeyedata-user-guide-zh.pdf` and opens offline from **Results & Trace > User Guide**. Its UI screenshots identify controls only; any visible test rows are not analysis evidence or a full-workflow acceptance claim. Synthetic scenarios, values, and reference tables support software-path checks only; device validation and physiological validity remain `NOT_RUN`.
