# Hermes 后台进程等待与原生续跑

## 问题与范围

Hermes 的 `terminal` 可启动带 `notify_on_complete=true` 的后台进程。主轮次结束后，原生网关在进程完成时投递新的自动通知 admission，继续原会话。引擎此前仅接入 `delegate_task` 后台委派等待，进程等待会进入普通任务结束检查，导致任务仍待验收时连续累计失败并进入 technical blocked。

本设计将有结构化完成通知约定的后台进程纳入现有逻辑执行生命周期，保持原任务、会话及执行身份，并在界面显示进程等待。任务重试上限、人工验收和历史任务状态沿用现有规则。

## 等待与结算

- 仅解析原生回执中 `terminal`、`process_manage` 的 tool 消息。有效 `proc_` 会话 ID 与 `notify_on_complete=true` 建立 `process:<ID>` 等待记录；显式关闭通知或进程终态清除对应等待。工具消息中的 `exit_code` 单独出现时沿用完成通知约定。
- 复用 `hermes_delegations`、绑定 generation 和当前业务归属校验。父轮次成功结束且存在等待时保持 `waiting_delegation`，保留任务占用与写入资格。该阶段跳过任务退出收尾，因此保持失败计数。
- 后台协调器只读查询原执行 home 下的 `state.db/session_admissions`，限定原生 session ID，并要求候选序号晚于已知原执行 admission。缺少原执行证据时保留等待并进入既有恢复流程。
- 候选通知须来自 `automation:` principal，持有 `local_automation_v1` 或 `native_text_v1.automation` 中与 request ID 一致的生产者身份。只接受已登记进程的完成、观察及心跳通知；进程完成身份携带有限数值的 spawn epoch。
- 观察和心跳回调继续等待，完成回调在其权威 terminal 回执到达后消耗等待。批量通知可一次结算多个进程。`hermes_background_callbacks` 持久化 admission 与等待记录映射，在数据库初始化时建表。
- 原生自动回调接入同一 `execution_id`，原生 request ID 保持原值；引擎只负责发现及查询回执。重放跳过已结算 admission。全部进程与子委派处理结束后执行一次逻辑收尾。

## 界面与停止

后台进程等待在工作会话和话题保留气泡，标签为“后台进程正在执行”；同时等待进程与子代理时显示“后台任务正在执行”。话题纯子代理等待继续显示既有标签。工作会话纯子代理等待保持既有展示规则。

等待时指示点静止，历史过程和停止入口可用。原生回调开始后恢复“思考中”，逻辑结束后显示完成。`background_kind` 随 `/thinking`、`/reasoning` 返回并纳入本地缓存和增量渲染签名。HTTP 查询仅读取引擎账本。

用户停止、任务移交、成员撤销和绑定换代复用现有委派取消与授权规则。历史 technical blocked 任务通过正常业务流程恢复；本补丁保持历史记录。

## 验证边界

隔离 SQLite 与模拟 RPC 验证等待、观察/完成、批量通知、混合委派、回放、可信身份、会话和序号边界、重启、停止及异常消息。真实 Chrome 使用全拦截合成 HTTP，覆盖深浅主题和 1280/390px 的工作进程与话题委派。原生通知身份格式另与匹配 Hermes 源码核对；外部模型推理与真实网关端到端执行属于独立验收范围。
