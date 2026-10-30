# PYeyedata

PYeyedata 是面向 Windows 10/11 x64 的桌面眼动数据分析软件。普通使用无需另装 Python、Qt、服务器或数据库。

## 下载与安装

**[下载最新版本](https://github.com/hexiovo/PYeyedata/releases/latest)**。推荐下载 `PYeyedata-1.0.0-windows-x64.zip`：完整解压后运行目录中的 `PYeyedata.exe`。也提供独立 EXE、源码包、校验和、构建记录，以及可单独下载的研究配置 skill 目录包和单文件版。

首次使用请先看[快速上手](docs/QUICKSTART.md)。它包含下载校验、完整解压、合成练习、文件类型说明和实验配置指引。ZIP 内也附有快速上手、部署说明、发布说明、指标/预处理指南、skill 文件和第三方许可证材料。

## 分析文件

- 按 **F1** 或选择 **帮助 → 新手入门与文件示例** 查看内置帮助。
- 使用 **帮助 → 导出练习资料与模板** 获取合成练习数据和空白模板。导入 `signals-practice.csv` 并加载 `pupil-mean-practice.pyeplan`，可以核对左瞳孔平均值约为 3.3 mm。此练习不代表真实设备验证。
- `.pyeplan` 保存分析设置，不包含输入数据或结果；`.pyea` 保存一次运行档案。导入配置、研究清单、数据文件与这两类文件用途不同。
- 配置方案通过 **文件 → 加载分析方案…** 使用；真实数据通过“数据导入”页打开，运行档案在“结果与追溯”中查看。

## 实验配置 skill

Release 同时提供 `PYeyedata-1.0.0-pyeyedata-study-config.zip` 和 `PYeyedata-1.0.0-pyeyedata-study-config.md`。目录版适合安装到支持 skill 目录的 AI 工具；单文件版可随实验设计、数据字典和脱敏样例一起作为附件提供。它会生成参数台账、输入转换计划、原生 `.pyeplan`、统计边界、阶段清单和零基础主试手册。

配置是否可加载必须由匹配源码和运行环境中的 PYeyedata 原生 API 检验；无法运行校验时要标为 `not_run`。结构校验不等于真实数据导入、统计设计成立或真实设备的小样本试运行通过。skill 不会替代主试确认单位、有效性、时间基准、独立单位等实验事实。

## 开发与发布

开发环境要求 Windows x64 CPython 3.12（最低 3.12.1，已验证 3.12.10）和 Git。进入仓库目录后运行：

```powershell
.\setup.cmd
.\start.cmd
```

准备依赖需要网络；环境只安装到项目 `.venv`。检查与发布步骤见[部署说明](docs/DEPLOYMENT.md)。

- [快速上手](docs/QUICKSTART.md)
- [部署、更新与发布说明](docs/DEPLOYMENT.md)
- [本版本说明](docs/RELEASE_NOTES.md)
- [项目接手指南](CODEX_HANDOFF.md)

## 许可与校验

应用随附的第三方许可证和来源说明可在程序的 **帮助 → 许可证** 中查看，便携包也附有相同材料。下载后按同一 Release 的 `SHA256SUMS.txt` 校验文件。项目自身未另行指定开源许可证；不得将第三方许可证解释为对项目代码的授权。
