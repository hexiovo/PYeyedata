# 研究配置包、清单与主试操作手册

## 配置包内容

只交付实验实际需要的文件。典型配置包如下：

```text
study-pack/
  study-pack-manifest.json       # 就绪状态、阶段、证据和文件索引
  parameter-ledger.csv           # 每项设置、来源和理由
  study-intake.md                # 已知事实、未知项、估计目标、设计和决策
  input-contract.md              # 文件/表格粒度、映射、单位、有效性、时间、ID
  import-profile.json            # 与 .pyeplan 分开；须有真实表头才能填写
  analysis-plan.pyeplan          # 仅含原生设置；由当前应用 API 保存
  source-contract.json           # 当前检出目录 API/schema 和源码指纹
  conversions/                   # 如有需要，放置实际研究转换脚本/报告
  statistics/                    # 仅在有需要时放置外部脚本/环境说明
  diagnostics/                   # 原生结构校验/合成诊断，明确标注类型
  operator-runbook.md            # 精确 GUI 操作、预期结果、停止和修复步骤
  evidence/                      # 依据、校验和、前检/小样本试运行回执
```

默认不把用户原始数据复制进配置包。导入配置不能混进方案设置；`.pyeplan` 不是完整项目；`.pyea` 是运行档案而不是配置。原生运行由应用保存为 `.pyea`，档案模式和位置由操作者决定。

## 研究清单结构

此清单由本 skill/编排层定义，不属于 PYeyedata。不得将其放入 `.pyeplan`。最小结构示例：

```json
{
  "schema": "pyeyedata-study-pack/v1",
  "study_id": "stable-local-id",
  "created_utc": "ISO-8601 timestamp",
  "readiness": {
    "configuration": "blocked|draft|structure_loadable",
    "run": "blocked|not_run|ready|running|complete|partial|failed",
    "real_pilot": "not_run|partial|passed|failed"
  },
  "source_contract": {
    "application_version": "...",
    "pipeline_schema_version": "...",
    "contract_version": "...",
    "fingerprint_sha256": "...",
    "snapshot_path": "source-contract.json"
  },
  "stage_manifest": [
    {"stage": "mapping", "status": "not_started|blocked|draft|ready|complete|partial|failed|not_applicable",
     "depends_on": [], "operation": "exact GUI action or literal command",
     "working_directory": "absolute path selected for the study pack or not_applicable",
     "environment": "exact executable/runtime and dependency lock, or not_applicable",
     "inputs": [], "outputs": [], "expected_output": "", "acceptance": [],
     "stop_and_remedy": [], "evidence_refs": [], "blockers": []}
  ],
  "parameter_ledger": [
    {"id": "stable-key", "stage": "processing", "parameter": "pupil_max_gap_s",
     "value": null, "unit": "s", "status": "known|proposed|missing|not_applicable",
     "evidence": [], "rationale": "", "impact_if_wrong": "", "native_config_path": "processing.pupil_max_gap_s"}
  ],
  "artifacts": [{"path": "analysis-plan.pyeplan", "role": "native-settings", "sha256": "...", "status": "validated"}],
  "blockers": [],
  "decisions": [],
  "evidence": []
}
```

实际清单必须填写单一真实枚举值，不能保留示例中的备选项。`created_utc` 使用生成时的真实时钟。`structure_loadable` 表示当前源码指纹匹配，且原生读取器和校验器接受该方案。`ready` 还要求操作者导入真实数据、确认独立映射和元数据并通过当前应用前检。`real_pilot=passed` 必须有真实小样本试运行文件、设备流程和留存证据；合成运行不能设为通过。

每个阶段都写明依赖、可复制的命令或精确 GUI 操作、工作目录、运行环境/依赖、预期输出、验收方式和停止/修复办法。未知信息应标为 `missing`，不适用项标 `not_applicable` 并说明理由；不得虚构路径/命令。台账每项只能标记 `known`、`proposed`、`missing` 或 `not_applicable`：`known` 必须附证据引用，`proposed` 是写清理由和确认人的临时选择，`missing` 会阻塞相关阶段，`not_applicable` 说明为何不适用。不能静默把建议升级成已知。

## 零基础主试操作手册

主试版操作手册默认使用简体中文。界面名称按当前 PYeyedata 版本核对。主窗口有七个分析页面，另有“运行日志”页：

| 界面步骤 | 主试操作 | 预期结果/通过标准 | 停止与处理方式 |
| --- | --- | --- | --- |
| **数据导入** | 打开真实信号表；选择工作表/分隔符；检查表头和样例行；每个必需源列仅映射一次；确认真实时间、瞳孔、坐标、有效性和 ID 含义；核对后另存导入配置。 | 导入预览能对应原始行；必需映射、单位和有效性状态可见；没有重复映射。 | 单位/代码未知、缺时间、列重复或缺失、解析警告解释不清时停止。向研究负责人/设备厂商索取代码本，修正映射并重新导入；不能按数值范围猜测。 |
| **数据预处理** | 查看质量和处理设置；记录当前参数后再加载研究方案；逐项核对参数台账；同时填写秒数和样本数限制；检查基线和滤波支持。 | 处理控件与已批准参数一致；质量报告保留原始值/来源有效性，并报告各分段实际应用/跳过状态。 | 采样、缺口、基线原点/覆盖率或 Nyquist 未知时停止。关闭无依据的操作，将阶段标为阻塞；补齐依据后重新生成/验证方案。 |
| **个体分析** | 只选择方案批准的个体指标 ID；核对指标需要的字段、眼别/单位、参数表单和上下文。 | 目录中的指标均已实现；所需上下文已接入；各指标单位和状态有定义。 | 方法未实现/实验功能未批准，或缺 AOI/事件/参考输入时停止；不能为通过界面检查随意替换指标。 |
| **多人分析** | 选择真实成员/交互和分析范围；检查时标对齐和重叠；确认同步方法和来源；设定群体指标参数。 | 成员键、时间范围、时间基准和重叠证据符合设计。 | 多个同时钟缺少同步依据，或团队/成员结构不清时停止。起始时刻相等、配对数相等不等于已同步，也不能证明独立样本数。 |
| **统计设计** | 声明设计、结果类型、独立单位、ID、组别/条件、绩效列与连接键、模型模板和预先指定的比较族。 | 每项声明都有方案/数据依据；目标推断在原生支持范围内；前检没有未解决的设计阻塞。 | 二元/计数/有界/删失/未知结果，或依赖关系未确认时停止；保留描述统计，把不支持的推断明确转为外部方法，不能重新标成连续。 |
| **高级指标** | 只启用研究方案要求的方法；按需附真实外部事件、目标运动、AOI、阅读标注、参考或同步来源。 | 来源 schema、单位、几何及方法来源通过当前可用性检查。 | 缺来源引用、AOI 几何未确认、事件覆盖未知或坐标转换不支持时停止。转换脚本只有通过基于实际来源样例的检查后才能使用。 |
| **结果与追溯** | 检查每行结果的单位、状态、有效 N、排除项、警告、统计审计和来源；将需要的 CSV/XLSX/PDF 导出到选定目录；保存 `.pyea` 运行档案。 | 输出表包含预期内容并保留 partial/unavailable 原因；档案模式和完整性有记录。 | 状态/单位缺失、结果与设计不符或导出丢审计时停止。保留日志，不能把 partial/failed 运行称为完成。 |
| **运行日志** | 出现警告或错误后打开此页，查看当前阶段、后端消息和错误回溯。 | 日志能指出当前操作或提供保留的诊断信息。 | 保存原始报错和源码指纹；按提示回到所属页面或输入进行修复，再运行前检和分析。日志页用于排障，不是科学分析阶段。 |

首次运行前，先点全局操作**检查可用条件**并阅读所有状态和原因。所有必需检查通过后再点**运行分析**；前检本身不会执行分析。保存设置用 **文件 → 保存分析方案…**（`Ctrl+Shift+S`），加载设置用 **文件 → 加载分析方案…**（`Ctrl+Shift+O`）。方案只替换设置；输入数据、历史记录、多人范围和同步证据仍需重新核对。不要查找或虚构 `--config` 命令行参数。

### 首次启动、文件类型和输出目录

- 使用发布便携包时，先完整解压，再从解压目录启动 `PYeyedata.exe`。使用源码目录时，在仓库根目录打开 PowerShell，运行 `.\start.cmd`；如当前目录尚无 `.venv`，先按项目说明运行 `.\setup.cmd`。常规研究操作通过 GUI 完成。
- 原始数据从**数据导入**打开；`.pyeplan` 从文件菜单加载；`.pyea` 是运行档案，在**结果与追溯**查看；导入配置在**数据导入**页选择。这些文件用途不同，不能放进错误的打开窗口。
- 在研究目录下新建由负责人选择的输出文件夹。原生设置保存为 `analysis-plan.pyeplan`，转换副本放 `converted/`，导出放 `exports/`，运行档案放 `runs/`。原始输入保持只读并放在生成文件夹之外，除非数据负责人明确要求复制。
- 如需在源码目录运行校验工具，从仓库根目录使用项目 Python 环境，例如：

  ```powershell
  .\.venv\Scripts\python.exe .agents\skills\pyeyedata-study-config\scripts\validate_plan.py export-default --project-root $PWD --out "<study-pack>\analysis-plan.pyeplan"
  .\.venv\Scripts\python.exe .agents\skills\pyeyedata-study-config\scripts\validate_plan.py validate "<study-pack>\analysis-plan.pyeplan" --project-root $PWD --snapshot "<study-pack>\source-contract.json"
  ```

  将 `<study-pack>` 换成实际目标目录，记录真实命令、工作目录和 Python 版本。这些命令只创建或校验设置与源码快照，不会导入数据、运行指标或证明研究已就绪。如果收到的是单文件版技能，将文件末尾嵌入的校验工具源码另存为 `validate_plan.py`，再配合选定的 PYeyedata 源码目录使用。没有匹配的源码和运行环境时，将原生校验标为 `not_run`。

### 外部转换或统计脚本

必须为确实需要的脚本提供实际命令、运行时和路径，例如：

```powershell
Set-Location "<study-pack-root>"
& "<approved-python-executable>" ".\conversions\convert_<study>.py" --input "<actual-source>" --output ".\converted\<output>"
```

操作手册还要写清真实可执行文件、Python/包版本和锁文件、参数、预期输出名、退出码含义、报告位置及修复办法。批量转换前用代表性样例验证。不能静默安装依赖，也不能保证项目环境一定能运行外部统计后端。缺少运行时或包时，停止并请负责人批准/安装；没有实际运行证据前，状态保持 `not_run`。

常见处理办法：

- 未知源列保持未映射；按真实代码本/表头修正后重新导入。
- 时间单位、有效性、坐标、必需上下文或同步阻塞需要证据，不能通过勾选绕过。
- `partial` 或 `unavailable` 可能是正确结果；保留原因，不能用 0 替换缺失结果。
- 方案被拒绝时，先确认应用/schema 兼容性并查看校验报告，再从当前应用导出基线、仅重新应用受支持字段并复验；不能随意删除未知原生字段。
- 配置或来源变化后重新跑当前前检，不能沿用旧的就绪回执。
- “保存分析方案”保存设置，“保存运行档案”保存运行。results-only 档案可能缺少重算所需数据；检查档案模式和完整性原因。
- 确实发生分析失败时，保留日志、精确源码指纹、参数台账、输入哈希和可用的 `.pyea`；不能把失败改标为完成。

## 示例场景

### 元数据缺失：正确阻断

输入：“用 EyeLink 导出数据比较两组瞳孔响应。”未提供文件、瞳孔语义/单位、真实表头/代码本、时间语义、组 ID 来源、独立单位说明、事件/窗口定义或结果类型。

预期配置包：研究信息摘要记录“两组比较”目标；台账将这些缺失事实标为 `missing`；映射/转换、瞳孔指标、统计和 `run` 就绪状态均阻断。可以包含未修改的原生默认值导出作为基线，但不能称作研究配置。提出简洁、针对性问题，同时继续准备数据清单和主试操作步骤。不能猜参数、伪造表头或声称可运行。

### 信息完整的简单设计：结构校验通过，但尚未进行真实试运行

仅作合成示例：一个左瞳孔平均值指标，连续结果，两个独立参与者组，以参与者 ID 为独立单位，组列 `condition`，并明确记录独立性依据。校验工具可以通过 `MainWindow.analysis_plan()` 导出当前默认值，仅更改所需字段，再用实时 schema 校验。产物标为 `synthetic_diagnostic`；因为没有真实数据、转换、GUI 前检或设备测试，`run=not_run` 且 `real_pilot=not_run`。真实使用前仍要确认瞳孔单位、时间元数据和真实组列来源。
