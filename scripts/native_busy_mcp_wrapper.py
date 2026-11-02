"""#276 真机探针：在引擎自带任务队列 MCP 上追加 busy_wait 受控工具。

serve() 把 task_queue 规范的 args 指到本包装器（不新增/修改编辑器 MCP 列表），引擎
mutate/resume 两分支的 spec 保持一致——原方案在 editor_mcp 追加独立服务器会与恢复投递的
resume 编辑器列表指纹失配，网关拒 acp_mcp_policy_conflict（真机证据）。

引擎 mcp/server.py 以脚本方式直接运行（模块级 mcp = FastMCP("group-engine")），本包装器
按文件路径加载该模块（import mcp.server 会命中 site-packages 的 SDK 包，不得用），在其
FastMCP 实例上追加 busy_wait 后沿用原 main() 启动（含心跳与 stdio 传输）。
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

_loader = importlib.util.spec_from_file_location('group_engine_mcp', ROOT / 'mcp' / 'server.py')
_group_mcp = importlib.util.module_from_spec(_loader)
sys.modules['group_engine_mcp'] = _group_mcp
_loader.loader.exec_module(_group_mcp)


@_group_mcp.mcp.tool()
def busy_wait() -> str:
    """Block until the isolated probe controller releases this execution."""
    root = Path(os.environ['NATIVE_BUSY_PROBE_WORK'])
    (root / 'ready_busy_wait').touch()
    deadline = time.monotonic() + 600
    while not (root / 'continue').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Probe controller did not release busy_wait')
        time.sleep(.2)
    return 'Released. Reply exactly once with [BUSY_DONE].'


if __name__ == '__main__':
    _group_mcp.main()
