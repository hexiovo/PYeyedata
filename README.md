# PYeyedata

PYeyedata is a Windows desktop application for eye-tracking data analysis. The supported operating systems are Windows 10 and Windows 11, x64.

## 下载即用 / 新主机开发

**[下载最新 Release](https://github.com/hexiovo/PYeyedata/releases/latest)**：选择 `windows-x64.zip`，解压后运行 `PYeyedata.exe`；不需要安装 Python。也提供独立 EXE、源码包、校验和与构建信息。

开发主机安装 Windows x64 Python 3.12（推荐 3.12.10）后，运行 `setup.cmd` 准备环境，`start.cmd` 启动，提交修改后用 `release.cmd` 检查并打包。

- [完整部署与发布说明](docs/DEPLOYMENT.md)
- [Codex 项目现状与接手指南](CODEX_HANDOFF.md)（另一台主机先读此文及根目录 AGENTS.md）
- [本次更新](docs/RELEASE_NOTES.md)

## 第一次使用

运行单文件 `PYeyedata.exe` 后，按 **F1** 或点 **帮助 → 新手入门与文件示例**。
用 **帮助 → 导出练习资料与模板** 得到一套合成练习数据、空白模板和通俗说明。
按练习导入 `signals-practice.csv` 并加载 `pupil-mean-practice.pyeplan`，可核对左瞳孔平均值约为 3.3 mm。

**文件 → 保存/加载分析方案** 用来复用设置；**结果与追溯 → 保存运行档案** 用来保留某次结果。
退出时如有未归档的运行，可以返回、逐个归档后退出，或明确选择不归档。
方案、结果导出和运行档案的区别，以及全部 10 类输入文件的填写实例，都在内置帮助和离线 PDF 手册里。

0.9.6 包含导入映射预设、逐文件批量队列、质量时间轴定位、静态图片 AOI 编辑、运行比较和缺失原因导航。具体操作及限制见[工作流补充说明](docs/WORKFLOW_ENHANCEMENTS.md)；便携 ZIP 随附此说明。内置 41 页 PDF 内容仍为 0.9.3，新增功能请阅读补充说明。

0.9.6 另新增 52 个常见眼动指标：事件分布统计、基于注视事件的 AOI 首次注视/时长、阅读时间与跳读/回视率，以及已观察试次/被试的 AOI 命中率。入口、阅读标注示例、分母与未知状态规则见[常见指标说明](docs/COMMON_METRICS.md)。原有 AOI 首次进入和访问指标保持原义。

## Build from source

Use 64-bit CPython 3.12.1 or a later 3.12.x release on a Windows 10/11 x64 build machine. CPython 3.12.10 is the validated runtime. Version 3.12.0 is below the build minimum because a code-object replacement bug can break PyInstaller's frozen SciPy startup. Install the pinned environment from `requirements.txt`, then run the checked-in one-file build runner with a new output directory outside the repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$releaseOutput = Join-Path $env:TEMP ("PYeyedata-release-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $releaseOutput | Out-Null
.\.venv\Scripts\python.exe -X utf8 -B .\build_windows.py --run $releaseOutput --phase final
```

The resulting `PYeyedata.exe`, build logs, and verification evidence are written under `$releaseOutput`. The recipe derives the project root from the checked-out source and does not require the original HexiPlan RUN or its audit files. PyInstaller Analysis omits only the reviewed API-set/UCRT names when their source is the build host's `System32`; same-named package files are retained, and any other System32-sourced binary fails the build. Windows 10/11 supplies the API-set contracts and Universal CRT used by the application.

The output records the actual build inputs and environment. Build artifacts are machine-specific; the procedure does not promise byte-identical EXEs across different Windows installations.

## Licenses

The application exposes its bundled third-party licenses and source notices through **Help → Licenses**. The source manifest identifies bundled runtime and resource components, their source references, and applicable notices. Review the Microsoft Visual C++ runtime conditions and component provenance before distributing a frozen binary outside the local application scope; the source tree and the build recipe do not establish a general binary redistribution right.
