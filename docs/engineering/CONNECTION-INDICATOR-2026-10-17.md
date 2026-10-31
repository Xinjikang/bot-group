# 连接状态灯修复（2026-10-17）

顶栏状态灯原先固定使用绿色，`setConn()` 切换的 `off` 类缺少对应样式，导致 SSE 断线重连期间仍显示绿色。

`frontend/index.html` 统一以 `data-state` 控制状态灯和文字：

| 状态 | 颜色与动效 | 文案 |
| --- | --- | --- |
| connecting | 黄色呼吸灯，无绿色光晕 | 引擎连接中… / 引擎断线重连中… |
| connected | 绿色呼吸灯与光晕 | 引擎已连接 |
| disconnected | 灰色静态灯 | 引擎已断开 |

引擎状态表示当前页面与 bot-group 引擎的 SSE 连接。相邻的 Hermes 网关状态表示引擎管理的网关连接池，所有状态文案均包含“Hermes 网关”对象名称。

初始 HTML 和每次创建 EventSource 都进入 connecting；onopen 进入 connected；onerror 根据 readyState 区分自动重连与 CLOSED；退出登录主动关闭连接时进入 disconnected。

验证时在隔离 Chrome 页面注入合成 EventSource 事件，检查两套主题的初始加载、刷新、连接、重连、关闭及替换连接，并核对计算后的背景色、动画与光晕。部署后刷新页面加载新资源。
