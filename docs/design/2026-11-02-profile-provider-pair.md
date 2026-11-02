# 创建 profile 的模型与供应商

Hermes、DSH 创建表单将模型与供应商置于配置来源下方的同一字段组，两项均必填。Codex 显示可选模型字段，供应商沿用来源或宿主默认。DSH 默认来源优先 desktop，运行组件继续使用 headless。

只读管理员接口 `POST /profiles/source-settings` 解析经过扫描匹配的 Hermes/DSH 来源，仅返回模型与供应商字符串。空来源按现有宿主默认来源语义读取；空配置显示空字段。扫描结果、来源、弹窗实例的过期响应均丢弃。手动编辑任一项后保留整组输入；跨宿主分别保存模型组草稿。

创建接口增加可选 provider。新版表单的 Hermes/DSH 请求显式提交 model/provider，服务端验证两项非空及单行约束，然后写入 Hermes model.default/model.provider 或 DSH agent-default-model.config.model/provider。旧客户端省略 provider 时保留既有继承语义和请求恢复指纹。Codex 界面省略 provider；程序调用显式提供时写入 model_provider。

供应商选择由配置决定，共享凭据只服务于已选供应商。项目级模型保存、全局勾选行为和启动 overlay 的优先级保持现有契约。验证涵盖来源带入、空来源补齐、跨宿主草稿、来源响应竞态、成对必填、实际落盘及原生 DSH 配置组合。
