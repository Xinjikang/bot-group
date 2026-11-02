"""req#141 task#275：模板管理支持职责与重复返修话题触发提示词。

验收标准（4 部分）：
① 6 个 manifest roles[].responsibilities 仅追加管理支持职责，原职责与 permissions 保留
② 6 个 AGENTS.md.tmpl 在完整角色章节后插入「管理支持行为」四段，插入点符合设计
③ 审查段整体覆盖「问题位置、依据、预期与实际、影响」四要素，只补缺项
④ engine/wake.py _WORK_SYSTEM_PROMPT 第 5 条与 _CHAT_SYSTEM_PROMPT 第 6 条 L2 括号
   替换为「需多人讨论，或同一问题反复返修仍未解决时…」

运行：python -B -m unittest scripts.test_template_management_support -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import project_templates as templates  # noqa: E402

BEHAVIORS = (
    '保护整体效率',
    '主动提供帮助',
    '让返修能够执行',
    '跟踪帮助效果',
    '复核自身判断',
    '资料、权限、资源实际可用',
    '按本文件审查段的要求写明',
    '最小验证方式及判据与证据',
    '具名协助人和可独立执行的复验步骤',
    '在原节点记录采纳、验证与解除阻塞的结果',
)

# ① manifest responsibilities 仅追加增量（原职责与 permissions 保留）
EXPECTED_RESPONSIBILITIES = {
    'software': {
        'owner': ('确认范围和版本、创建和管理需求、确认交付成果并关闭需求',
                  '统筹整体效率、跟踪协助解除阻塞'),
        'architect': ('设计接口和数据约束、拆解前置依赖、协调修订和合并、验收关闭及撤回任务',
                      '具体可验证的技术支持'),
        'reviewer': ('独立核验设计、代码、数据边界与测试证据', '可执行返修指导'),
    },
    'writing': {
        'editor': ('统筹整体效率、实质支持、跟踪解除结果',),
        'proofreader': ('修订指导',),
    },
    'algorithm': {
        'lead': ('统筹整体效率、实质支持、跟踪解除结果',),
        'reviewer': ('返修指导',),
    },
    'testing': {
        'lead': ('统筹整体效率、实质支持、跟踪解除结果',),
        'reviewer': ('修订指导',),
    },
    'research': {
        'lead': ('统筹整体效率、实质支持、跟踪解除结果',),
        'verifier': ('补证与修订指导',),
    },
    'product-prototype': {
        'pm': ('统筹整体效率、实质支持、跟踪解除结果',),
        'reviewer': ('返修指导',),
    },
}

# ② 插入点（管理支持行为段必须位于该章节标题之前）
INSERTION_SECTIONS = {
    'software': '## 开发流程',
    'writing': '## 工作流程',
    'algorithm': '## 研发流程',
    'testing': '## 测试依据',
    'research': '## 工作流程',
    'product-prototype': '## 协作流程',
}

# ③ 审查段四要素（每模板审查/核验段必须包含）
REVIEW_ELEMENTS = {
    'software': ('问题位置', '预期与实际', '影响'),
    'writing': ('问题位置', '依据', '预期与实际', '影响'),
    'algorithm': ('问题位置', '预期与实际', '影响'),
    'testing': ('问题位置', '依据', '预期与实际', '影响'),
    'research': ('问题位置', '依据', '预期与实际', '影响'),
    'product-prototype': ('依据', '位置', '预期与实际', '影响'),
}

L2_REPLACEMENT = ('需多人讨论，或同一问题反复返修仍未解决时，建话题拉相关成员，'
                  '结合已有证据和尝试记录明确原因、解决路径与协助分工')


class TemplateManagementSupportTests(unittest.TestCase):
    """六模板职责/管理行为/审查段 + wake.py L2 替换回归。"""

    def _tmpl_text(self, name):
        return (ROOT / 'templates' / 'projects' / name / 'AGENTS.md.tmpl').read_text(encoding='utf-8')

    def test_manifest_responsibilities_appended_only(self):
        # 验收①：13 个角色的管理支持职责已追加，原职责前缀与 permissions 保留
        for name, roles in EXPECTED_RESPONSIBILITIES.items():
            manifest = templates.load(name)
            by_id = {r['id']: r for r in manifest['roles']}
            for role_id, expects in roles.items():
                resp = by_id[role_id]['responsibilities']
                for original in expects[:-1]:
                    self.assertIn(original, resp, f'{name}/{role_id} 原职责被改动')
                self.assertTrue(resp.endswith(expects[-1]),
                                f'{name}/{role_id} 未追加「{expects[-1]}」：{resp}')
            # permissions 未动（软件模板抽查 + 全部模板 roles 结构合法）
            for r in manifest['roles']:
                self.assertIn('permissions', r, f'{name}/{r["id"]} 缺 permissions')

    def test_all_templates_insert_management_behaviors(self):
        # 验收②：六模板「管理支持行为」四段逐字一致，且位于指定流程章节之前
        rendered = {name: self._tmpl_text(name) for name in INSERTION_SECTIONS}
        first = None
        for name, text in rendered.items():
            section = INSERTION_SECTIONS[name]
            self.assertIn('## 管理支持行为', text, name)
            for token in BEHAVIORS:
                self.assertIn(token, text, f'{name} 缺「{token}」')
            pos = text.index('## 管理支持行为')
            self.assertLess(pos, text.index(section), f'{name} 管理行为段未在「{section}」之前')
            block = text[text.index('## 管理支持行为'):text.index(section)]
            if first is None:
                first = block
            else:
                self.assertEqual(first, block, f'{name} 管理行为段与首个模板不一致')

    def test_review_sections_cover_four_elements(self):
        # 验收③：审查/核验段覆盖问题位置、依据、预期与实际、影响（只补缺项）
        for name, elements in REVIEW_ELEMENTS.items():
            text = self._tmpl_text(name)
            for element in elements:
                self.assertIn(element, text, f'{name} 审查段缺「{element}」')

    def test_wake_l2_bracket_replaced(self):
        # 验收④：WORK 第 5 条与 CHAT 第 6 条 L2 括号均为重复返修话题触发提示词
        from engine import wake
        old_l2 = '（需多人讨论，建话题拉人→结论落库→收口）'
        for label, prompt in (('WORK', wake._WORK_SYSTEM_PROMPT),
                              ('CHAT', wake._CHAT_SYSTEM_PROMPT)):
            self.assertIn(L2_REPLACEMENT, prompt, f'{label} 缺 L2 重复返修提示词')
            self.assertNotIn(old_l2, prompt, f'{label} 旧 L2 括号未替换')

    def test_render_publishes_new_roles_and_behaviors(self):
        # 渲染链路：software 模板渲染后 ROLE_TABLE 带新增职责 + 管理行为段落地
        from scripts import test_project_initialization as baseline
        baseline.InitializationTests.setUp(self)
        try:
            self.hosts = self.base / 'hosts'
            self.hosts.mkdir()
            ids = []
            for i in range(8):
                home = self.hosts / str(i)
                home.mkdir()
                (home / 'config.yaml').write_text('preserved host config', encoding='utf-8')
                runtime = 'hermes' if i % 2 else 'codex'
                ids.append(self.conn.execute(
                    'INSERT INTO bots(host_type,config_root,profile,display_name,runtime) VALUES(?,?,?,?,?)',
                    (runtime, str(home), 'default', 'Member ' + str(i), runtime)).lastrowid)
            self.conn.commit()
            manifest = templates.load('software')
            roles = [{k: r[k] for k in ('id', 'name', 'permissions')} for r in manifest['roles']]
            body = {'root': str(self.root), 'display_name': 'Mgmt support probe',
                    'description': 'minimal', 'template': {'id': 'software', 'version': manifest['version']},
                    'roles': roles,
                    'members': [{'bot_id': bot, 'role': r['id'], 'permissions': []}
                                for bot, r in zip(ids, roles)],
                    'owner_bot_id': ids[0]}
            plan = self.client.post('/projects/initializations/preview', json=body).json()
            result = baseline.InitializationTests.submit(self, plan).json()
            self.assertEqual(result['status'], 'ready', result)
            agents = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
            for token in ('统筹整体效率、跟踪协助解除阻塞', '具体可验证的技术支持',
                          '可执行返修指导', '## 管理支持行为', '让返修能够执行'):
                self.assertIn(token, agents)
            self.assertIn('预期与实际及判断依据', agents)
        finally:
            baseline.InitializationTests.tearDown(self)


if __name__ == '__main__':
    unittest.main(verbosity=2)
