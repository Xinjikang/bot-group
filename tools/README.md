# 安装、发布与维护工具

从项目根目录执行 `python tools/<文件>.py`，或使用根目录的 BAT / shell 入口。

| 工具 | 职责 |
| --- | --- |
| `install.py` | 创建与检查运行环境、依赖及配置，修复已有安装 |
| `bootstrap.py` | 校验并安装 Release 源码包 |
| `make_release.py`、`releases.py` | 构建源码资产，解析发布索引并校验版本与摘要 |
| `upgrade.py` | 停机、备份、升级和回滚；支持指定旧安装目录迁移布局 |
| `service.py`、`start_engine.py`、`stop_engine.py`、`restart_engine.py` | 服务启停与重启回执 |
| `repair_hermes_identity.py` | 指定旧身份的检查与显式修复，默认预览 |

`scripts/` 保存测试、测试支持和验证探针。发布与维护步骤见 [发布指南](../docs/guides/RELEASE-GUIDE.md)，执行测试前阅读 [测试索引](../scripts/TEST_INDEX.md)。
