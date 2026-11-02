"""#277 前端载荷实采：隔离夹具经真实 api.get_thinking/get_reasoning 生成观察载荷。

输出单行 JSON：{first_thinking, second_thinking, old_reasoning, new_reasoning,
new_reasoning_ended}——覆盖 node1715 反例时序（t1000 busy→t1005 idle→t1006 busy→
t1010 idle），供 scripts/test_native_observation_lifecycle.cjs 消费（生产前端函数
vm 载荷验证，旧批折叠时新批仍活跃）。

隔离：临时 SQLite + 原生状态打桩 + 受控时钟，不触达真实网关/进程/生产数据。
运行：PYTHONPATH= .venv/Scripts/python.exe -X utf8 -B scripts/native_observation_payloads.py
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import api, db, heartbeat, hermes_gateway, native_observation, reasoning, wake  # noqa: E402
from scripts import test_native_observation as observation_tests  # noqa: E402


def produce() -> dict:
    class Harness(observation_tests.NativeObservationTests):
        def runTest(self):
            pass

    harness = Harness()
    harness.setUp()
    try:
        harness.bind('dev-1', session_id='nat-a')
        now = [1000.0]
        with patch.object(native_observation.time, 'time', side_effect=lambda: now[0]), \
                patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=[]), \
                patch.object(wake, '_running_wakes', {}), \
                patch.object(reasoning, 'read_reasoning',
                             side_effect=lambda *a, **kw: {'entries': [], 'latest_id': 0}):
            harness.states['dev-1-p-work'] = 'native_running'
            first_thinking = api.get_thinking()          # 第一批 started 1000
            now[0] = 1005.0
            harness.states['dev-1-p-work'] = 'idle'
            api.get_thinking()                            # 第一批 ended 1005
            now[0] = 1006.0
            harness.states['dev-1-p-work'] = 'native_running'
            api.get_thinking()                            # 第二批 started 1006
            second_thinking = api.get_thinking()
            old_reasoning = api.get_reasoning('dev-1', 'dev-1-p-work', 1000.0)   # 旧批迟到查询
            new_reasoning = api.get_reasoning('dev-1', 'dev-1-p-work', 1006.0)   # 新批仍活跃
            now[0] = 1010.0
            harness.states['dev-1-p-work'] = 'idle'
            api.get_thinking()                            # 第二批 ended 1010
            new_reasoning_ended = api.get_reasoning('dev-1', 'dev-1-p-work', 1006.0)
        return {'first_thinking': first_thinking, 'second_thinking': second_thinking,
                'old_reasoning': old_reasoning, 'new_reasoning': new_reasoning,
                'new_reasoning_ended': new_reasoning_ended}
    finally:
        harness.doCleanups()


if __name__ == '__main__':
    print(json.dumps(produce(), ensure_ascii=False, default=str))
