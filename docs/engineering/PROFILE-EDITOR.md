# 成员 Profile 编辑

当前入口：项目详情 → 成员与目录 → 成员卡片“编辑”。支持 Hermes/Codex，描述、SOUL/AGENTS、环境配置与模型字段按宿主能力展示。
保存成功关闭编辑窗口并返回成员列表；取消丢弃草稿，失败保留输入。

## 配置作用域

模型、供应商与推理强度默认保存本项目覆盖；勾选“应用到全局”时更新全局并清空当前项目这三个字段的覆盖。描述、SOUL/AGENTS 和环境配置走全局成员配置。两个作用域使用各自 revision，部分成功时保留草稿并提示具体阶段。详见 [统一模型编辑](UNIFIED-MODEL-EDITOR.md)、[项目模型](PROJECT-MEMBER-MODELS-2026-10-11.md)。

Codex 配置由 `engine/codex_profile_config.py` 读取所选 home/命名配置及 AGENTS，保存保留其他用户配置；home 与命名配置规则见 [Codex 配置目录](../guides/CODEX-PROFILE-HOMES.md)。以下文件与备份细节描述 Hermes 分支。

## Hermes 文件与生效方式

- 描述：`profile.yaml` 的 `description`，手动修改后将 `description_auto` 置为 false。
- SOUL.md、.env：编辑原文，保留原文件换行风格。
- config.yaml：替换 `model.default`、`model.provider` 的单行值；文件、`model` 段或其中字段缺失时，首次保存自动补齐。新建空白 profile 可直接填写模型和供应商后保存。
  插入沿用原有缩进、BOM 和换行风格，已有注释和其他配置字节保持原样。多行值、YAML 引用及行内映射等不适合精确修改的结构仍拒绝保存。
- Profile 根目录遵循 Hermes 的 `HERMES_HOME` 规则；`default` 使用根目录，命名 profile 使用 `profiles/<name>`。
- 推理强度对应 `agent.reasoning_effort`，有效支持范围由宿主、模型和供应商决定。
- 保存过程中保持现有进程运行，后续 Hermes 新进程加载保存后的文件；spawn 实现保持原样。

## 备份与恢复

每次保存只备份实际发生变化的文件：修改描述备份 `profile.yaml`，修改 SOUL.md 或 .env 备份对应文件，修改模型或供应商备份 `config.yaml`。同时修改多项时备份对应的几份文件；内容完全未变时不生成备份。原始字节备份至：

```text
<profile 目录>/.group-engine-backups/<UTC 时间戳>-<随机后缀>/
```

备份长期保留。`manifest.json` 记录本次涉及的文件、这些文件的版本校验值及原先不存在的文件。
备份写入并回读验证成功后才开始替换原文件；备份失败即中止保存。
保存失败时尝试恢复已经替换的文件，持久备份仍然保留。
编辑期间检测到其他操作修改文件时返回 409，重新打开编辑窗口后再编辑。
跨文件写入采用逐文件原子替换和失败恢复，进程异常终止时可从持久备份手动恢复。

## 接口与验证

- `GET /members/{profile}/config`：按宿主读取配置字段、能力与 `revision`，响应禁止缓存。
- `PUT /members/{profile}/config`：提交修改字段与读取时的 `revision`，按宿主保存并返回结果；字段细节以对应配置模块为准。
- 两个接口使用管理员认证，拒绝 bot 身份；未设管理员密码时沿用本地开发模式。
- 验证入口：`scripts.test_profile_config`、`scripts.test_codex_profile_config`、`scripts.test_project_models` 及对应模型浏览器用例，运行条件见 [测试索引](../../scripts/TEST_INDEX.md)。

首次保存模型的用例覆盖空白新建、来源缺少模型段、单字段缺失、HTTP 创建后保存/回读、精确插入及持久备份，使用临时 profile 和数据库。

更新后安装 `requirements.txt` 中依赖，重启引擎并刷新页面。
