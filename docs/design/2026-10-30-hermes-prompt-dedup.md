# Hermes 会话规则的持久化判重

## 契约与依据

依据 HERMES-GATEWAY.md 的规则刷新契约及 2026-10-28-hermes-gateway-cancel-cleanup.md §7.4，同 sid 复用已保存的系统提示词。初始规则通过 session.create 的 client_env 注入系统层；后续配置变化通过用户消息补充。新建会话重新建立系统层规则。原生 launch_request 隐去 client_env，判重使用本地持久化记录。

按 profile_id、session_id 保存 system_hash 与 user_hash，分别表示已确认创建时的系统规则和最近受理的用户层规则。哈希覆盖规则正文 UTF-8 字节；消息、任务 ID、时间戳与凭据保持在 signal 或执行环境中。有 user_hash 时只比较 user_hash，否则比较 system_hash。A→B→A 必须再次注入 A。

## 执行与恢复

每次需要补充规则时，在 prompt.submit 前将候选哈希与 execution_id 原子保存。收到身份校验通过的原生受理回执后，在同一数据库事务中更新执行状态和 user_hash。回执未知保持候选状态；已知 admission ID 时复用原对账流程。首个回执完全丢失且 admission ID 未知时保留占用并进入人工核对，禁止自动重发。明确未提交时维持原哈希，下次合法唤醒重新判断。

受理记录按持久序号排序，重复或迟到回执保持幂等。原生 rewind 删除含规则的用户消息后撤销对应用户层记录，恢复最近仍有效的已受理版本。已有会话和迁移会话保持未知 system_hash，首次补充当前规则到用户层，受理后记录 user_hash。新建会话成功后记录 system_hash；创建回执丢失后作为未知旧会话兼容补齐。

排队期间取消且原生执行代数始终为空时，撤销对应用户层记录，下一轮重新补齐。

唤醒沿用现有与 /new 共用的串行作用域锁，待核对执行保持会话占用。/new 创建独立 sid 并注入当前项目规则。旧 sid 的记录保持隔离。

## 验收

覆盖首建、稳定配置连续唤醒、A→B→A、旧会话和迁移补齐、/new、跨 profile 隔离、进程重启恢复、受理后回执丢失、未知首回执、未提交、重复/迟到/非法回执、事务回滚及停止回退。出站夹具核对每次用户消息是否携带规则，并验证 launch_request 缺少 client_env 时行为正确。
