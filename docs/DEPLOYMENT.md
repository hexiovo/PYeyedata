# 下载、部署与发布

## 普通使用：下载后解压运行

打开[最新版本下载页](https://github.com/hexiovo/PYeyedata/releases/latest)，下载 `PYeyedata-1.0.0-windows-x64.zip`。先完整解压，再运行目录中的 `PYeyedata.exe`。适用于 Windows 10/11 x64；无需安装 Python、Qt、数据库或服务。首次启动会将运行环境解压到临时目录，请预留磁盘空间。不要在压缩包预览窗口内运行。

同一 Release 还提供独立 EXE、源码 ZIP、研究配置 skill 目录 ZIP、完整内嵌指引的单文件 Markdown、`release-info.json`、`native-components.json` 和 `SHA256SUMS.txt`。便携 ZIP 内附快速上手、部署/版本说明、预处理/指标文档、研究配置 skill 和第三方许可证。应用未进行代码签名；下载后请用 `SHA256SUMS.txt` 核对文件：

```powershell
Get-FileHash .\PYeyedata-1.0.0-windows-x64.zip -Algorithm SHA256
```

将输出与同一 Release 的 `SHA256SUMS.txt` 中 ZIP 对应行比较。按 F1 或选择**帮助 → 新手入门与文件示例**开始；也可从帮助菜单导出练习数据和模板。随附中文图解手册内容版本仍为 0.9.3。

升级时将新版本解压到新文件夹再启动。配置、缓存和日志位于 `%LOCALAPPDATA%\PYeyedata\`；输入数据、导出、`.pyea` 档案和 `.pyeplan` 方案仍在原保存位置。删除旧程序文件不会自动删除用户数据；复制程序到另一台电脑也不会自动搬移数据。

## 开发环境

开发机需要 Windows x64 CPython 3.12.1 或更高的 3.12.x 版本（已验证 3.12.10）和 Git。3.12.0、3.13/3.14 与 32 位 Python 不符合当前构建配方。克隆仓库后，在项目根目录运行：

```powershell
.\setup.cmd     # 创建本机 .venv，安装 requirements.txt 锁定版本并运行 pip check
.\start.cmd     # 启动程序；若环境尚未准备，会先进行准备
```

首次安装依赖需要网络，只作用于当前项目 `.venv`，不修改系统 Python。可用 `setup.cmd -Python "C:\path\to\python.exe"` 指定解释器。每台电脑单独创建 `.venv`，不要复制其他电脑的虚拟环境。更换依赖后重新运行 `setup.cmd`。

源码版可通过工具栏或**帮助**菜单打开“环境检查与依赖安装”。窗口检查 Python 3.12 x64、锁定版本、关键模块导入和 `pip check`；环境异常时可修复当前项目 `.venv`。EXE 仅检查随包内置的依赖，不启动 pip。修复后要关闭程序，并通过 `start.cmd` 重新启动。若缺少启动所需的 Qt 等依赖而无法打开窗口，运行 `setup.cmd`。

## 本地构建正式发布

发布包要求干净 Git 工作区和已提交的源码。输出目录必须是源码目录外**尚不存在的目录**；构建过程中不要改动源码或版本信息。提交全部发布输入后运行：

```powershell
.\release.cmd -Output "D:\releases\PYeyedata-1.0.0"
```

`release.cmd` 会先检查源码，再构建最终 EXE，并对冻结程序运行相同的三项固定诊断；最后核对 PE 版本、内嵌构建清单、原生依赖/许可证映射并打包。构建完成后检查输出目录的 `assets\release-info.json`、`assets\SHA256SUMS.txt` 和日志。源码与冻结检查报告分别位于 `source-checks/`、`frozen-checks/`；构建中间文件留在输出目录供排错，不要上传这些目录。

Release 资产包括：

- `PYeyedata-1.0.0-windows-x64.zip`：便携程序、部署文档、skill 目录和许可证。
- `PYeyedata-1.0.0-windows-x64.exe`：独立程序文件。
- `PYeyedata-1.0.0-source.zip`：与本次 EXE 相同提交的源码。
- `PYeyedata-1.0.0-pyeyedata-study-config.zip`：可单独安装的完整 skill 目录。
- `PYeyedata-1.0.0-pyeyedata-study-config.md`：含所有指引和 helper 源码的单文件版。
- `release-info.json`、`native-components.json` 和 `SHA256SUMS.txt`：构建记录、原生依赖映射和所有发布文件的校验和。

发布前同步 `APP_VERSION`、`version.md`、`docs/RELEASE_NOTES.md`、本文件、快速上手和接手文档，以及 `resource-index.json` / `source-manifest.json` 的版本与索引哈希。skill 或分发文档有变化时，也更新 build manifest 输入清单。图解手册内容未改时保留其 `guide_version`。数据、算法、缓存、`.pyea` 和 `.pyeplan` 契约版本不要随应用版本误升。

## 发布到 GitHub

确认构建资产和校验和后，推送精确提交，再为该提交建立与 `APP_VERSION` 完全匹配的标签，上传 `assets/` 内所有正式文件，并使用 `docs/RELEASE_NOTES.md` 作为版本说明。不要上传 `.venv`、临时数据、诊断工作目录或未列入 `assets/` 的构建中间文件。

```powershell
git push origin master
git tag v1.0.0
git push origin v1.0.0
$releaseAssets = Get-ChildItem -File 'D:\releases\PYeyedata-1.0.0\assets' | ForEach-Object FullName
gh release create v1.0.0 @releaseAssets --verify-tag --title 'PYeyedata v1.0.0' --notes-file docs/RELEASE_NOTES.md
```

发布前先确认远端尚无相同标签/Release，避免重复创建。也可以在 GitHub Actions 手动运行 **Windows release**，输入已推送且与 `APP_VERSION` 一致的标签；推送标签本身不会自动触发打包。远程工作流须以其实际运行结果判断，不因本机发布成功而推定远程构建已通过。

## 许可证与验证范围

`native-components.json` 对应实际构建中的原生组件路径和哈希；`source-manifest.json` 及程序“帮助 → 许可证”列出第三方来源和适用说明。原生组件变化时须先更新来源映射，发布检查才能通过。项目本身未另行指定开源许可证；不要替作者添加授权声明。

发布检查覆盖当前主机上的源码/冻结固定诊断、合成数据和离屏 GUI 操作，不等于真实设备科学验证，也不代表另一台干净 Windows 主机的首次启动、原生多屏或不同 DPI 验收。设备兼容性与科学适用范围须根据实际采集和研究设计另行确认。
