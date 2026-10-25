# 维护模块与宿主包隔离

安装、发布、升级及服务管理的实现位于 `engine/maintenance/`，按 `engine.maintenance.<模块>` 导入。包初始化仅包含模块说明，基础 Python 可在依赖安装前加载维护入口。

`tools/` 保存现有命令行入口，脚本将程序根目录加入导入路径后调用对应维护模块，保留参数和退出码语义。入口覆盖 install、bootstrap、make_release、upgrade、update_engine、service、start_engine、stop_engine、restart_engine 和 repair_hermes_identity。发布索引解析模块为 `engine.maintenance.releases`。

Hermes 的 SessionDB 适配器在引擎进程中导入 Hermes 源码。Hermes 使用顶层 `tools` 包，因此项目维护模块必须使用 `engine.maintenance`；`tools/` 保持普通目录，维护模块保持宿主 `sys.modules['tools']` 的身份与内容。

## 发布与旧版升级

- 源码清单包含 `engine/maintenance/` 和 `tools/` 命令入口。
- 独立发布资产 `bootstrap.py` 直接复制 `engine/maintenance/bootstrap.py`。该实现仅使用标准库，下载源码之前即可执行。
- v0.1.x 兼容入口 `scripts/install.py` 调用 `tools/install.py`；`scripts/service.py` 从 `engine.maintenance.service` 导入 `_load_env`。
- 旧受管的 `tools/__init__.py` 与 `tools/releases.py` 按升级清单删除，回滚时恢复旧快照。Git 源码更新后，管理员重启引擎以加载新的模块集合。

## 验证

`scripts/test_maintenance_namespace.py` 在同一子进程中分别执行维护模块先加载、Hermes 先加载两种顺序，使用当前安装的真实 Hermes 源码及临时 SessionDB 验证更新检查、创建、新建与恢复会话，以及重启/升级模块共存。整个子进程与其他测试隔离，两套被测模块共用解释器。设置 `HERMES_TEST_SOURCE` 为宿主源码根启用这两项测试。

同一套件还验证从其他工作目录通过基础解释器运行 CLI 帮助、源码清单和公开 bootstrap 单文件执行。`test_app_upgrade_integration.py` 从迁移前提交 `1ac85a2d4fccc751343c940c3312f660314bd67d` 构造旧安装，通过旧版本实际 API 和升级助手验证迁移、就绪失败回滚及成功后的运行；安装依赖采用受控夹具。更多命令见 [测试索引](../../scripts/TEST_INDEX.md)。
