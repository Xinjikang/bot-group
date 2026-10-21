"""req#113 task#194：模板角色改名后流程文档按角色 ID 统一渲染。

验收标准：
① 把 architect 显示名改为 DesignLead 后，生成的 AGENTS/README/workflow 中架构师引用
   与角色表一致（均 DesignLead，无「架构师」旧称残留）
② 授权链路不受影响（create_role/assign_role 仍按新名正确落 role_id）
③ 默认角色模板（不改名）行为不变

运行：python -B -m unittest scripts.test_template_role_rename -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import project_templates as templates  # noqa: E402
from scripts import test_project_initialization as baseline  # noqa: E402


class TemplateRoleRenameTests(unittest.TestCase):
    def setUp(self):
        baseline.InitializationTests.setUp(self)
        self.ids = []
        self.hosts = self.base / 'hosts'
        self.hosts.mkdir()
        for i in range(8):
            home = self.hosts / str(i)
            home.mkdir()
            (home / 'config.yaml').write_text('preserved host config', encoding='utf-8')
            runtime = 'hermes' if i % 2 else 'codex'
            self.ids.append(self.conn.execute(
                'INSERT INTO bots(host_type,config_root,profile,display_name,runtime) VALUES(?,?,?,?,?)',
                (runtime, str(home), 'default', 'Member ' + str(i), runtime)).lastrowid)
        self.conn.commit()

    def body(self, name='software', rename=None):
        manifest = templates.load(name)
        roles = [{k: r[k] for k in ('id', 'name', 'permissions')} for r in manifest['roles']]
        if rename:
            for r in roles:
                if r['id'] == rename[0]:
                    r['name'] = rename[1]
        return {'root': str(self.root), 'display_name': 'Isolated ' + name,
                'description': 'Minimal verified scenario',
                'template': {'id': name, 'version': manifest['version']}, 'roles': roles,
                'members': [{'bot_id': bot, 'role': r['id'], 'permissions': []}
                            for bot, r in zip(self.ids, roles)],
                'owner_bot_id': self.ids[0]}

    def preview(self, body):
        return self.client.post('/projects/initializations/preview', json=body)

    def submit(self, plan):
        return baseline.InitializationTests.submit(self, plan)

    def test_architect_rename_propagates_to_workflow_docs(self):
        # 验收①：architect → DesignLead，AGENTS/README/workflow 无「架构师」旧称残留
        plan = self.preview(self.body('software', rename=('architect', 'DesignLead'))).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        agents = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
        readme = (self.root / 'README.md').read_text(encoding='utf-8')
        self.assertNotIn('架构师', agents)
        self.assertNotIn('架构师', readme)
        self.assertIn('DesignLead', agents)
        self.assertIn('[项目协作约定](AGENTS.md)', readme)
        workflow = result['template']['workflow']
        self.assertNotIn('架构师', workflow)
        self.assertIn('DesignLead', workflow)
        self.assertNotIn('{{ROLE_', workflow)  # 无未渲染变量残留

    def test_reviewer_rename_preserves_review_delivery_requirements(self):
        plan = self.preview(self.body('software', rename=('reviewer', 'QualityLead'))).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        agents = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('QualityLead每次初审或复审交接前', agents)
        self.assertNotIn('审查员', agents)
        for text in ('result 写明', '被审提交 ID', '问题位置、影响、修订要求和复审条件',
                     '保留当前任务归属', 'logged=true'):
            self.assertIn(text, agents)

    def test_rename_does_not_break_authorization(self):
        # 验收②：授权链路不受影响，create_role/assign_role 按新名正确落 role_id
        plan = self.preview(self.body('software', rename=('architect', 'DesignLead'))).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        project = result['project']
        row = self.conn.execute(
            "SELECT id FROM project_roles WHERE project=? AND name='DesignLead'", (project,)).fetchone()
        self.assertIsNotNone(row)
        # 分配 architect 角色的成员 role_id 指向 DesignLead 角色实体
        self.assertEqual(result['applied_team']['role_ids']['architect'], row['id'])
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM project_roles WHERE project=? AND name='架构师'", (project,)).fetchone())

    def test_default_template_unchanged(self):
        # 验收③：默认角色模板（不改名）行为不变，流程引用渲染成原始角色名
        plan = self.preview(self.body('software')).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        agents = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('项目负责人建需求', agents)
        self.assertIn('架构师拆解', agents)
        self.assertIn('开发自测交付', agents)
        self.assertNotIn('{{ROLE_', agents)
        workflow = result['template']['workflow']
        self.assertIn('架构师拆解', workflow)
        self.assertIn('架构师验收关闭任务', workflow)
        self.assertIn('项目负责人关闭需求', workflow)
        self.assertNotIn('{{ROLE_', workflow)

    def test_research_owner_rename_covers_duties_and_escalation(self):
        plan = self.preview(self.body('research', rename=('lead', 'ResearchLead'))).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        agents = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
        for text in ('ResearchLead是本项目的负责人', '技术障碍先协调当前ResearchLead',
                     'ResearchLead验收报告并关闭任务和需求', '接收并跟进任务阻塞通知',
                     '接手人由ResearchLead维护', '交ResearchLead确认修改'):
            self.assertIn(text, agents)
        self.assertNotIn('调研负责人', agents)
        self.assertNotIn('{{ROLE_', agents)

    def test_writing_editor_rename_covers_management_and_execution_boundaries(self):
        plan = self.preview(self.body('writing', rename=('editor', 'EditorLead'))).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        agents = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
        for text in ('EditorLead是本项目的负责人', '技术障碍先协调当前EditorLead',
                     '接手人由EditorLead维护', '资料研究员、作者和审校的默认管理权限为空'):
            self.assertIn(text, agents)
        self.assertNotIn('{{ROLE_', agents)


if __name__ == '__main__':
    unittest.main()
