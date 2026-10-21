# Windows 公开安装入口

用户在 PowerShell 或 CMD 执行 README 提供的一条命令，将正式 Release 的 `install.ps1` 下载到本地执行。脚本读取组织仓库最新正式发布索引，固定本次版本、源码 URL 与 SHA256，并下载和校验对应版本的独立 `bootstrap.py`。bootstrap 获取源码，调用现有 install 流程创建专用 venv、安装锁定依赖并自检。

支持 64 位 Windows 10/11、Windows PowerShell 5.1。安装器优先选择安装目录已有 venv、显式 PythonPath、Python launcher 中的 3.13/3.12/3.11、PATH 中兼容的 Python。缺失时下载 Python 官方 3.13.16 x64 安装程序并核验官方 SHA256，在当前用户目录安装运行时；系统 PATH 保持原值。Python 资产和摘要来源为 https://www.python.org/downloads/release/python-31316/ 。

默认程序目录为 `%LOCALAPPDATA%\bot-group\app`，可用 `INSTALL_DIR` 或脚本参数覆盖。安装与启动分开，安装后输出 start/stop/upgrade 入口。BAT 优先使用安装目录 venv，允许系统 PATH 中没有 Python。默认升级源为组织仓库最新正式发布索引，CLI 与 `RELEASE_INDEX_URL` 保留覆盖能力。

重复安装复用现有配置和数据，同版本中断可重跑。目标目录存在不同版本源码时由 bootstrap 拒绝覆盖，使用 `upgrade.bat` 完成受控升级。正式 Release 资产绑定 tag 与固定版本 URL，包含源码 ZIP、外置清单、SHA256SUMS、releases.json、bootstrap.py、install.ps1。源码 ZIP 保留双语 README。

发布验收包含两层：隔离测试验证参数、失败与数据保护；实际公开地址使用 README 中的命令完成下载、全新 venv 安装、启动、浏览器页面、停止和默认升级查询。公开地址的验证结果是安装交付完成的必要条件。
