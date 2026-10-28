# 下载、部署与开发

## 直接使用：不安装 Python

打开 [最新 Release](https://github.com/hexiovo/PYeyedata/releases/latest)，下载 `PYeyedata-0.9.7-windows-x64.zip`，解压后双击 `PYeyedata.exe`。也可直接下载同版本的 `windows-x64.exe` 单文件运行。系统要求 Windows 10/11 x64，无需管理员权限、Python、Qt 安装或额外服务。首次启动需解压内置运行库，请留出临时磁盘空间。

程序目前没有代码签名；若系统提示来源，先确认来自上述仓库并核对 `SHA256SUMS.txt`。PowerShell 校验下载文件：

```powershell
Get-FileHash .\PYeyedata-0.9.7-windows-x64.zip -Algorithm SHA256
```

将哈希与同一 Release 的 `SHA256SUMS.txt` 对应行比较。不要在 ZIP 压缩窗口中直接运行。按 F1 或工具栏“全流程图解手册（PDF）”开始；可从帮助菜单导出练习与模板。应用 0.9.7 随附指南内容版本 0.9.3。

升级时解压新版本到新目录并运行。配置、缓存、日志在 `%LOCALAPPDATA%\PYeyedata\`；输入、导出、`.pyea` 档案和 `.pyeplan` 方案仍在自己保存的位置。删除旧 EXE 不会自动删除用户数据。复制程序到另一台主机不会自动带走这些数据，所需档案请自行复制。

## 开发主机：三条入口

安装 **Windows x64 CPython 3.12.1 或以上的 3.12 系列**（推荐已验证的 3.12.10）与 Git。3.12.0、3.13/3.14 和 32 位 Python 不支持本构建配方。克隆仓库后：

```powershell
.\setup.cmd    # 创建本地 .venv，安装 requirements.txt 精确版本，pip check
.\start.cmd    # 启动；缺少 .venv 时自动准备环境
.\release.cmd  # 先提交全部修改；检查、构建、检查 EXE、打包
```

首次准备需要网络获取依赖，不修改系统 Python 环境。Python 未安装时脚本会提示安装地址。可用 `setup.cmd -Python "C:\Python312\python.exe"` 明确解释器。每台主机创建自己的 `.venv`，不要复制另一台主机的虚拟环境。更换依赖后重新运行 `setup.cmd`。

只检查源码（不打包）或指定发布输出目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/manage.ps1 -Action Check
.\release.cmd -Output "D:\releases\PYeyedata-0.9.7"
```

发布目录必须是源码目录以外的**全新目录**，默认使用系统临时目录并打印位置。打包时需 Git 和干净提交；下载的 GitHub source.zip 可启动开发，但正式打包应使用 Git 克隆。临时诊断数据和构建文件保留在输出目录供排错，上传仅用其中 `assets/`。不要在构建过程中修改源码或版本记录。

## 发布维护

1. 更新 `APP_VERSION`、`version.md`、`docs/RELEASE_NOTES.md`、本文的版本示例和接手文档；同步资源索引/许可证来源清单版本以及被索引清单的 SHA-256。指南内容没改时保留其 guide_version。
2. 提交到 `master`，运行 `release.cmd`。源码和冻结 EXE 各执行三项固定诊断，构建验证 PE 版本与完整输入哈希。检查 `assets/release-info.json` 与实际输出。
3. 推送 `master`，对这次打包的精确提交创建并推送 `v<APP_VERSION>` 标签；使用 GitHub Release 上传 `assets/` 下所有文件，说明使用 `docs/RELEASE_NOTES.md`。不要上传 `.venv`、临时数据或工作目录。

```powershell
git push origin master
git tag v0.9.7
git push origin v0.9.7
$releaseAssets = Get-ChildItem -File 'D:\releases\PYeyedata-0.9.7\assets' | ForEach-Object FullName
gh release create v0.9.7 @releaseAssets --verify-tag --title 'PYeyedata v0.9.7' --notes-file docs/RELEASE_NOTES.md
```

以上版本只作当前版示例，已有 Release 不要重复创建。也可在 GitHub Actions 手动运行 **Windows release**，输入已推送且与 APP_VERSION 一致的新标签；远程 Windows 2022 构建机使用 Python 3.12.10、相同脚本并发布 Release。本地发布与远程发布二选一，工作流不会因推送标签自动重复打包。远程工作流尚需在 GitHub 实际运行后才能确认该运行器的兼容性。

Release 包含独立 EXE、便携 ZIP、精确提交 source.zip、校验和和构建信息。ZIP 内附部署说明及第三方许可证，EXE 的帮助菜单也能查看内置许可证。`native-components.json` 记录与实际构建逐一核对的原生组件哈希和声明映射，`source-manifest.json` 给出第三方来源和源码链接。原生组件变化时须先维护来源映射才能通过发布检查。本仓库没有给自身代码额外指定开源许可证，不要自行替作者添加授权。

目前的检查是同主机构建、合成数据和离屏 GUI 检查，不代替真实设备科学验证或另一台干净 Windows 的首次启动、原生多屏/DPI 验收。启动失败先查看 `%LOCALAPPDATA%\PYeyedata\logs\`；诊断失败查看脚本输出目录下对应 `.log` 和 `result.json`。

启动入口会核对实际加载的应用模块来源及包搜索目录，拒绝混用不同程序目录。固定检查从源码目录外运行同一 launcher，在 `result.json` 的 `process.module_origins` 及 `summary.json` 保留实际来源路径；这是模块解析检查，内容哈希仍由构建清单负责。会话缓存只在当前进程内复用；日志同时说明实际命中和未复用原因，重启不会自动从 checkpoint 接续。档案和方案的读取沿用各自校验契约。
