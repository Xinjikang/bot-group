# 连续唤醒的提示词与工具前缀验证

## 范围

共享 `_MCP_TOOLS` 增加 `operation_get`，同时覆盖 Codex `enabled_tools` 与 Hermes 子进程 `tools.include`。通过注册工具集合回归检查白名单完整性。

固定项目配置、工作目录与会话，连续执行两轮，改变用户信号及执行凭据。验证规则正文、运行时系统文本及工具 schema 的 SHA-256。规则按原始 UTF-8 字节计算；结构化 schema 使用键排序、紧凑 JSON，保留数组顺序、名称、描述和参数全部内容。Codex 分别记录顶层 instructions 与 developer 消息，Hermes 记录 system 消息。保留两份原始请求及可阅读的文本，避免仅比较工具名或传输配置。

## 验证边界

适配层用例执行真实唤醒装配并替换进程或 RPC，检查动态信号与静态规则分离。原生探针使用隔离 home、数据库、工作目录和随机回环模型端点；MCP 启动仓库实际服务，只枚举工具。Codex 执行首建与 resume；Hermes 使用原生 AIAgent 和 SessionDB，每轮重建 agent，从数据库恢复历史与冻结前缀，并覆盖子进程与会话专属 MCP 命名。

回环模型固定返回文本，验证不调用外部模型、不执行业务工具。Hermes 探针覆盖模型请求构造和持久会话恢复；网关 WS 受理与租约生命周期由既有隔离回归覆盖。两轮相同的哈希证明本次固定版本与配置下的前缀稳定，配置变更、压缩和运行时升级属于独立边界。

Codex 部分版本把基础指令和项目规则都编码为 developer 消息，顶层 instructions 为空；以完整 developer 消息数组的哈希验证该层，并单独保存原始规则文本。Hermes 延迟加载时，模型请求包含 tool_search/tool_describe/tool_call 及目录描述；同时验证这些出站 schema 和原生 MCP discovery 得到的完整 manifest。第二轮从 SessionDB 恢复首轮用户消息和回复，并对重新构建 system 的路径设置失败断言。
