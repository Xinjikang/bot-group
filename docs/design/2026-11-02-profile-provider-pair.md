# 创建 profile 的模型与供应商

模型与供应商位于默认收起的“更多设置”中。Hermes、DSH 同组展示这两个可选字段；Codex 展示可选模型字段，供应商沿用来源或宿主默认。DSH 默认来源优先 desktop，运行组件继续使用 headless。

只读管理员接口 `POST /profiles/source-settings` 解析经过扫描匹配的 Hermes/DSH 来源，仅返回模型与供应商字符串。“使用默认设置”读取 Hermes 主 profile 或同一 DSH HOME 的 desktop 主 profile；DSH desktop 缺失时保留原生 bundle 默认。预览与创建共用来源解析规则。空配置显示空字段。扫描结果、来源、弹窗实例的过期响应均丢弃。手动编辑任一项后保留整组输入；跨宿主分别保存模型组草稿。

创建接口接受可选 model/provider。两项均留空时继承来源配置；单项填写时只覆盖对应项，另一项保留来源值。来源自身缺少字段时沿用宿主默认处理。服务端保留单行约束，并将显式值写入 Hermes model.default/model.provider 或 DSH agent-default-model.config.model/provider。旧客户端省略 provider 时保留既有继承语义和请求恢复指纹。Codex 界面省略 provider；程序调用显式提供时写入 model_provider。

供应商选择由配置决定，共享凭据只服务于已选供应商。项目级模型保存、全局勾选行为和启动 overlay 的优先级保持现有契约。验证涵盖更多设置默认收起、来源带入、两项留空继承、单项覆盖、默认主 profile、缺少默认字段、跨宿主草稿、来源响应竞态及实际落盘。
