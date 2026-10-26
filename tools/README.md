# 安装、发布与维护工具

从项目根目录执行 `python tools/<文件>.py`，或使用根目录的 BAT / shell 入口。

本目录保存命令行转发入口。可导入的实现位于 `engine/maintenance/`，调用方使用 `engine.maintenance` 命名空间；`tools/` 保持普通目录，以兼容同进程加载的 Hermes `tools` 包。模块契约见 [维护模块](../docs/engineering/MAINTENANCE-MODULES.md)。

| 工具 | 职责 |
| --- | --- |
| `install.py` | 创建与检查运行环境、依赖及配置，维护 Windows 桌面快捷方式，修复已有安装 |
| `bootstrap.py` | 校验并安装 Release 源码包 |
| `make_release.py` | 构建源码资产；索引解析与摘要校验由 `engine.maintenance.releases` 提供 |
| `upgrade.py` | 停机、备份、升级和回滚；旧版用户沿用安装目录的 `upgrade.bat` |
| `update_engine.py` | 执行网页确认的升级，持久化进度并启动升级后或恢复后的服务 |
| `service.py`、`start_engine.py`、`stop_engine.py`、`restart_engine.py` | 启动复用同一实例并打开 UI，维护流程完整重启，记录重启回执 |
| `repair_hermes_identity.py` | 指定旧身份的检查与显式修复，默认预览 |

`scripts/` 保存测试、测试支持和验证探针。发布与维护步骤见 [发布指南](../docs/guides/RELEASE-GUIDE.md)，执行测试前阅读 [测试索引](../scripts/TEST_INDEX.md)。

源码包额外生成 `scripts/install.py` 与 `scripts/service.py` 两个旧版升级兼容入口，分别调用 `tools/install.py` 与 `engine.maintenance.service`，并纳入发布文件清单。公开下载的独立 `bootstrap.py` 资产来自 `engine/maintenance/bootstrap.py`，仅依赖 Python 标准库。
