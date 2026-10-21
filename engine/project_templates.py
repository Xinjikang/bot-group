"""Repository templates and immutable application snapshots; no Bot file writes."""
import json
from pathlib import Path, PurePosixPath
import re
import stat

from fastapi import HTTPException
from . import db, bot_identity, project_team, project_permissions as permissions
from . import project_workspaces as ws, taskpool

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / 'templates' / 'projects'
VARIABLES = {'PROJECT_NAME', 'PROJECT_DESCRIPTION', 'PROJECT_ID', 'PROJECT_ROOT',
             'DIRECTORY_MODE', 'ROLE_TABLE', 'WORKSPACE_RULES'}
TOKEN = re.compile(r'\{\{([A-Z_]+)\}\}')
IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]{0,63}')


def invalid(message):
    raise HTTPException(400, {'code': 'invalid_template', 'message': message})


def relative_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\\:*?"<>|') or any(ord(c) < 32 for c in value):
        invalid('模板路径必须是项目内的相对路径')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) or p.upper().split('.')[0] in
           {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}
           for p in parts) or (any(p.startswith('.') for p in parts) and value not in {'internal/.gitignore', 'reports/.gitignore'}):
        invalid('模板路径包含越界或保留目录')
    return PurePosixPath(value)


def resource(base, relative):
    path = base.joinpath(*relative_path(relative).parts)
    # Check every ancestor: resolve alone would follow a junction silently.
    for part in [*reversed(path.parents), path]:
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                invalid('模板资源包含符号链接或重解析点')
    if not path.resolve().is_relative_to(base.resolve()) or not path.is_file() or path.stat().st_size > 131072:
        invalid('模板资源缺失、越界或超过 128 KiB')
    return path


def text_resource(base, relative):
    try:
        return resource(base, relative).read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        invalid(f'模板资源读取失败：{type(exc).__name__}')


def render(text, variables, role_ids=frozenset()):
    """渲染模板变量。

    role_ids 是允许的 {{ROLE_<ID>}} 大写角色局部 ID 集合（req#113：角色改名后
    流程文档按角色 ID 统一渲染）。静态变量见 VARIABLES，ROLE_<ID> 动态变量按
    配置后角色名映射生成（见 prepare 的 variables）。
    """
    def allowed(name):
        return name in VARIABLES or (name.startswith('ROLE_') and name[5:] in role_ids)

    unknown = {t for t in TOKEN.findall(text) if not allowed(t)}
    residue = TOKEN.sub('', text)
    if unknown or '{{' in residue or '}}' in residue:
        invalid('模板包含未允许变量：' + ', '.join(sorted(unknown)))
    return TOKEN.sub(lambda m: variables[m[1]], text)


def permission_list(value):
    if not isinstance(value, list) or any(not isinstance(p, str) or p not in permissions.PERMISSIONS for p in value):
        invalid('权限必须来自当前项目权限列表')
    return sorted(set(value))


def load(template_id, version=None):
    if not isinstance(template_id, str) or not IDENTIFIER.fullmatch(template_id):
        invalid('模板 ID 格式错误')
    base = TEMPLATE_ROOT / template_id
    try:
        manifest = json.loads(text_resource(base, 'manifest.json'))
    except json.JSONDecodeError:
        invalid('模板 manifest JSON 格式错误')
    fields = {'id', 'version', 'name', 'description', 'roles', 'files', 'directory_modes', 'workflow'}
    if not isinstance(manifest, dict) or set(manifest) - {'owner_role_id'} != fields:
        invalid('模板 manifest 字段不完整')
    if manifest['id'] != template_id or not isinstance(manifest['version'], str) or not re.fullmatch(r'\d+\.\d+\.\d+', manifest['version']):
        invalid('模板 ID 或版本错误')
    if version is not None and version != manifest['version']:
        invalid('模板版本已变化，请重新选择')
    if any(not isinstance(manifest[k], str) or not manifest[k].strip() for k in ('name', 'description', 'workflow')):
        invalid('模板名称、简介和流程必须为文本')
    if not isinstance(manifest['directory_modes'], list) or not manifest['directory_modes'] or any(m not in ('shared', 'worktree') for m in manifest['directory_modes']):
        invalid('模板目录模式错误')
    roles = manifest['roles']
    if not isinstance(roles, list) or not 1 <= len(roles) <= 100:
        invalid('模板角色清单错误')
    role_ids, names, references = set(), set(), []
    for role in roles:
        if not isinstance(role, dict) or set(role) != {'id', 'name', 'responsibilities', 'permissions', 'required', 'soul_reference'}:
            invalid('角色字段不完整')
        if not isinstance(role['id'], str) or not IDENTIFIER.fullmatch(role['id']) or role['id'] in role_ids:
            invalid('角色 ID 重复或格式错误')
        role_ids.add(role['id'])
        if any(not isinstance(role[k], str) or not role[k].strip() for k in ('name', 'responsibilities')) or role['name'] in names or type(role['required']) is not bool:
            invalid('角色名称、职责或必需标记错误')
        names.add(role['name'])
        permission_list(role['permissions'])
        content = text_resource(base, role['soul_reference'])
        render(content, dict.fromkeys(VARIABLES, ''))
        references.append({'role_id': role['id'], 'name': role['name'], 'responsibilities': role['responsibilities'], 'content': content})
    if 'owner_role_id' in manifest and (not isinstance(manifest['owner_role_id'], str)
                                       or manifest['owner_role_id'] not in role_ids):
        invalid('负责人角色必须引用模板中的角色 ID')
    # req#113：允许的 {{ROLE_<ID>}} 大写角色局部 ID 集合；workflow 与 files 用角色 ID 占位须在此校验合法。
    role_ids_upper = {rid.upper() for rid in role_ids}
    empty_vars = dict.fromkeys(VARIABLES, '')
    empty_vars.update({f'ROLE_{rid}': '' for rid in role_ids_upper})
    render(manifest['workflow'], empty_vars, role_ids_upper)
    files = manifest['files']
    if not isinstance(files, list) or not 3 <= len(files) <= 64:
        invalid('模板文件清单错误')
    targets, contents = set(), {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {'source', 'target'}:
            invalid('文件需要 source 和 target')
        target = str(relative_path(item['target']))
        if target.casefold() in targets:
            invalid('模板目标文件重复')
        targets.add(target.casefold())
        content = text_resource(base, item['source'])
        render(content, empty_vars, role_ids_upper)
        contents[target] = content
    if not {'agents.md', 'readme.md', 'docs/index.md'}.issubset(targets):
        invalid('模板必须提供三份基础文件')
    for target in targets:
        if any(str(parent).casefold() in targets for parent in PurePosixPath(target).parents if str(parent) != '.'):
            invalid('模板文件与父目录冲突')
    return {**manifest, 'references': references, 'contents': contents}


def catalogue():
    templates = [load(name) for name in ('software', 'research', 'writing', 'product-prototype', 'algorithm')]
    return {'templates': [{k: t[k] for k in ('id', 'version', 'name', 'description', 'directory_modes')} for t in templates]}


def owner_role_name(template):
    return next((r['name'] for r in template['roles'] if r['id'] == template.get('owner_role_id')), '项目负责人')


def detail(template_id):
    template = load(template_id)
    template.pop('contents')
    conn = db.get_connection()
    try:
        template['bots'] = [dict(r) for r in conn.execute(
            "SELECT b.id AS bot_id,b.profile AS host_profile,b.display_name,b.runtime,m.profile AS member_profile "
            "FROM bots b LEFT JOIN members m ON m.bot_id=b.id WHERE b.profile<>'user' ORDER BY b.id")]
    finally:
        conn.close()
    template['permissions'] = [{'key': k, 'label': v} for k, v in permissions.PERMISSIONS.items()]
    return template


def validate_bots(conn, team):
    for member in team['members']:
        bot = conn.execute('SELECT id,profile,runtime,config_root,display_name FROM bots WHERE id=?', (member['bot_id'],)).fetchone()
        if bot is None or db.is_builtin_user(bot['profile']):
            invalid('所选 Bot 已失效，请返回重新选择')
        if bot['runtime'] not in ('hermes', 'codex') or not bot['config_root'] or not Path(bot['config_root']).is_dir():
            invalid('所选 Bot 宿主目录不可用，请重新选择')
        identity = {k: bot[k] for k in ('id', 'profile', 'runtime', 'config_root', 'display_name')}
        if member.get('identity') and member['identity'] != identity:
            invalid('所选 Bot 绑定已变化，请重新预览')
        member['identity'] = identity
        member['display_name'] = bot['display_name'] or bot['profile']


def workspace_rules(template_id, mode, roles):
    if template_id == 'algorithm':
        names = {r['id']: r['name'] for r in roles}
        if mode == 'shared':
            return (f"共享目录串行研发，一次只允许一名执行者修改文件。由{names['lead']}协调写入占用与交接，"
                    "核对改动归属和实验资源，交接时记录代码提交、文档版本和实验编号。"
                    "源码、Demo、配置、数据和模型目录沿用用户的实际工程结构；报告写入项目主目录 reports。")
        return f'''使用引擎分配的独立 Git worktree 工作区。工作区的创建、分配和清理由引擎管理。{names['lead']}确认基线分支、基线提交、工程结构、实验资源与远端同步约定。

开始前检查实际目录、分支、HEAD、任务归属和已有改动。需要修改文件的成员从指定基线或上游交付提交建立 `task/<任务ID>-<阶段>` 分支；续做时使用原分支，detached HEAD 时，先建立任务分支。分支已被其他工作区占用时由负责人协调。

{names['researcher']}提交文献、算法方案和实验计划，{names['engineer']}依据准确提交实现并自测，代码和版本化文档一起交付。源码、Demo、配置、数据与模型位置遵循实际工程约定，实验资源按任务分配。

{names['reviewer']}在自己的工作区核对准确提交及对应方案，独立审查与复现，评审结果写入任务节点。修订交回具体成员并按新提交复审。{names['lead']}核对通过评审的提交并合并，冲突解决涉及的变化补充评审和必要复验，再组织用户验收。

实验记录和最终报告统一写入引擎注入的项目主目录 reports，按研究和实验编号分别保存并备份。交接记录分支、准确提交、方案版本、实验编号、数据与模型版本、报告绝对路径、验证结果和具体接手人。当前工作区中的报告骨架作为格式参考，实际证据通过项目主目录路径交接。远端同步依已确认约定执行；清理前确认成果已集成且报告与证据可追溯。'''
    if template_id == 'product-prototype':
        names = {r['id']: r['name'] for r in roles}
        if mode == 'shared':
            return (f"共享目录串行开发，一次只允许一名执行者修改文件。由{names['pm']}协调写入占用与交接；"
                    "核对改动归属，交接时记录设计文档和原型版本后释放占用。"
                    "使用 Git 时，将本人负责的 docs、materials、ui-spec 与 outputs 中的文档和原型一起提交，并记录提交 ID。")
        return f'''使用引擎分配的独立 Git worktree 工作区。工作区的创建、分配和清理由引擎管理。{names['pm']}确认并登记项目基线分支、基线提交、文档与原型版本及远端同步约定。

开始工作前，核对实际目录、当前分支、HEAD、任务归属及已有改动。需要修改文件的成员从任务指定的基线或上游交付提交建立任务分支，分支采用 `task/<任务ID>-<阶段>`；续做时使用原阶段分支。工作区处于 detached HEAD 时，先建立任务分支再修改文件。分支命名冲突或已被其他工作区占用时，由{names['pm']}协调处理。

{names['pm']}和{names['ux']}按职责提交需求、研究、体验方案和 UI 规范，交接记录用户确认状态、文档版本及准确提交 ID。{names['prototyper']}以任务指定且包含已确认设计的提交为基础实现原型。`docs/`、`materials/`、`ui-spec/`、`outputs/` 中的相关文档、素材和原型按任务一起提交，保持 PRD、体验方案、UI 规范和原型版本对应。

{names['reviewer']}在自己的工作区检出交付的准确提交，依据对应版本的需求和设计操作原型，核验需求覆盖、交互、视觉一致性及可用性。评审结果写入任务节点；实现问题交{names['prototyper']}，体验与 UI 方案问题交{names['ux']}，需求及范围问题交{names['pm']}。修订完成后提交新的提交 ID，按影响范围复审。

{names['pm']}核对通过评审的提交并合并到约定基线，以合并后的版本组织用户走查。确认的调整由对应成员修订并按影响范围复审。最终核对用户反馈、PRD、体验方案、UI 规范、原型和覆盖表的一致性，再按项目流程验收关闭任务和需求。冲突解决涉及的变化补充评审及必要验证。

每次交接记录分支、提交 ID、文档与原型版本、成果路径、验证结果、遗留问题和具体接手人。同仓库工作区通过本地提交交接，远端同步按已确认的约定执行。清理前确认成果已集成、剩余改动归属明确，并保留可追溯的提交与任务记录。'''
    if template_id == 'research' and mode == 'worktree':
        names = {r['id']: r['name'] for r in roles}
        return f'''使用引擎分配的独立 Git worktree 工作区。工作区的创建、分配和清理由引擎管理。{names['lead']}确认并登记项目基线分支、基线提交、成果目录和远端同步约定。

开始工作前，核对实际目录、当前分支、HEAD、任务归属及已有改动。{names['researcher']}、{names['analyst']}从任务指定的基线或上游交付提交建立任务分支，分支采用 `task/<任务ID>-<阶段>`；续做时使用原阶段分支。工作区处于 detached HEAD 时，先建立任务分支再修改文件。分支命名冲突或已被其他工作区占用时，由{names['lead']}协调处理。

{names['researcher']}提交来源记录和采集资料，交接时写明文件路径、准确提交 ID、来源可靠性及资料缺口。{names['analyst']}以该交付提交为基础开展分析，保留来源引用，区分事实、推断和待验证假设。需求说明、资料、分析稿和报告按项目目录约定随任务提交。

{names['verifier']}在自己的工作区检出交付的准确提交，核对来源、证据与结论之间的对应关系，复核结果写入任务节点。需要修订时，交回具体负责成员，并记录问题、修订要求和复核条件；修订完成后提交新的提交 ID，再次复核。

{names['lead']}核对通过复核的提交，将成果合并到约定基线，检查合并后的文档、引用和附件是否完整一致，再按项目流程验收关闭任务和需求。新增内容及冲突解决涉及的变化，按影响范围补充复核。

每次交接记录分支、提交 ID、成果路径、当前状态、遗留问题和具体接手人。同仓库工作区通过本地提交交接，远端同步按已确认的约定执行。清理前确认成果已集成、剩余改动归属明确，并保留可追溯的提交与任务记录。'''
    if template_id == 'writing' and mode == 'worktree':
        names = {r['id']: r['name'] for r in roles}
        return f'''使用引擎分配的独立 Git worktree 工作区。工作区的创建、分配和清理由引擎管理。{names['editor']}确认并登记项目基线分支、基线提交、稿件目录和远端同步约定。

开始工作前，核对实际目录、当前分支、HEAD、任务归属及已有改动。{names['researcher']}、{names['writer']}从任务指定的基线或上游交付提交建立任务分支，分支采用 `task/<任务ID>-<阶段>`；续做时使用原阶段分支。工作区处于 detached HEAD 时，先建立任务分支再修改文件。分支命名冲突或已被其他工作区占用时，由{names['editor']}协调处理。

{names['researcher']}提交来源记录和相关资料，交接时说明来源、引用范围及待核实事项。{names['writer']}以该交付提交为基础维护提纲和稿件，保持引用与来源记录对应。选题简报、资料、提纲、稿件及配套素材按项目目录约定随任务提交，文件明确标注当前状态。

{names['proofreader']}在自己的工作区检出交付的准确提交，核查事实、引用、结构、语言和目标读者适配情况，审校结果写入任务节点。{names['writer']}依据意见修订，并逐项记录处理结果；涉及资料来源的问题交{names['researcher']}核实。修订完成后提交新的提交 ID，由{names['proofreader']}按影响范围复核。

{names['editor']}核对通过审校的提交，将稿件和配套资料合并到约定基线，检查最终内容、引用、素材及文档状态，再验收定稿并按项目流程关闭任务和需求。合并后的内容调整及冲突解决涉及的变化，按影响范围补充审校。对外发布按用户授权执行。

每次交接记录分支、提交 ID、稿件路径、当前状态、遗留问题和具体接手人。同仓库工作区通过本地提交交接，远端同步按已确认的约定执行。清理前确认成果已集成、剩余改动归属明确，并保留可追溯的提交与任务记录。'''
    if template_id != 'software':
        return ('共享目录串行开发，一次只允许一名执行者修改文件。交接前提交代码并记录提交 ID。'
                if mode == 'shared' else
                '按引擎分配的独立 Git worktree 工作。开发从 release/<版本> 创建 feature/<任务>，独立审查后由架构师合并 release，测试员验证版本，协调者确认后发布 master。远端、版本、环境和命令由项目填写。')
    if mode == 'shared':
        return '''共享目录串行开发，一次只允许一名执行者修改文件。开始前协调写入占用，核对当前分支、提交 ID 和工作区变更归属。
在引擎指定的共用项目主目录开展工作；分支由团队统一协调。交接前检查差异，只提交本人负责的变更，记录提交 ID、待处理改动及其归属，再释放写入占用。
可能影响他人的服务启停、数据清理先协调占用人，交接时记录仍需保留的服务与验证数据。'''
    names = {r['id']: r['name'] for r in roles}
    return f'''使用引擎分配的独立 Git worktree 工作区；开始前核对实际目录、任务分支与基线提交。工作区创建、分配和清理由引擎管理。
初始只有 main 时，由{names['architect']}依据用户确认的版本号，从 main 创建 release/<版本号>（例如 release/v0.1.0），并在工程文档登记版本、基线提交及远端约定。导入项目按用户确认的发布主分支建立基线。当前版本所有 feature 和 fix 成果持续合并到同一条 release 分支。
开发成员（dev，包括{names['backend']}和{names['frontend']}）开始修改代码前，必须在已分配的工作区内，从当前 release 基线创建并切换到 feature/<任务ID>-<模块>-<功能简述>（例如 feature/123-user-login）；缺陷修复使用 fix/<任务ID>-<问题简述>。续做已有任务时，核对分支归属与基线后切换到原任务分支。工作区初始为 detached HEAD 时，先完成分支创建和切换，再修改代码；版本基线尚未明确时，先与{names['architect']}确认。
创建或切换任务分支前，核对同名分支对应的任务、交付提交和 worktree 占用情况。同一任务的已有分支按续做处理；同名异任务或分支已被其他 worktree 检出时，保留已有分支及工作区，交{names['architect']}协调命名或占用移交，确认后再操作。
{names['architect']}将已确认的设计提交纳入 release，任务记录设计路径和提交 ID；开发建立任务分支时，确认基线包含所需设计和已完成的前置成果。开发交付记录分支名、准确提交 ID、自测结果与遗留问题。
{names['reviewer']}在自己的工作区以 detached HEAD 检出交付提交进行审查。完成 Git 操作前检查后，可使用 `git switch --detach <交付提交ID>`，并核对 HEAD 与交付记录一致。审查通过后，由{names['architect']}核对被审提交并合并到 release，合并对象绑定审查通过的准确提交 ID。任务分支出现新增提交或历史改写时，拟合入的变更须重新审查；记录实际合入的被审提交和合并后 release 提交，完成与改动匹配的必要验证并验收关闭开发任务。冲突由相关开发成员与{names['architect']}协同处理，冲突解决产生的变更补充审查和必要自测。
整个 release 开发合并完成后，由用户发出测试指令，{names['owner']}创建独立测试需求。{names['tester']}以 detached HEAD 检出指定 release 提交，验证合并后的版本，报告绑定被测提交 ID；发现问题只记录报告，修复与复测等待用户后续明确指令。版本验收并获得用户发布授权后，由指定负责人将 release 合并到发布主分支，登记发布提交或标签；下一版本从更新后的主分支建立新的 release。
同仓库本地 worktree 通过本地分支和提交交接；配置远端的项目按约定 push/fetch。每次交接记录分支名和提交 ID。各任务验收后保留提交与验证证据，检查剩余改动归属，由引擎按项目约定清理工作区。'''


def prepare(conn, body, config, project_id):
    selected = body.get('template')
    if not isinstance(selected, dict) or set(selected) != {'id', 'version'}:
        invalid('template 需要 id 和 version')
    template = load(selected['id'], selected['version'])
    if config['directory_mode'] not in template['directory_modes']:
        invalid('模板与所选目录模式不兼容')
    configured = body.get('roles', [{k: r[k] for k in ('id', 'name', 'permissions')} for r in template['roles']])
    if not isinstance(configured, list) or len(configured) != len(template['roles']):
        invalid('请保留模板的完整角色清单')
    roles, names, ids = [], set(), set()
    originals = {r['id']: r for r in template['roles']}
    for role in configured:
        if not isinstance(role, dict) or set(role) != {'id', 'name', 'permissions'} or not isinstance(role['id'], str) or role['id'] not in originals or role['id'] in ids:
            invalid('角色必须来自模板，使用唯一局部 ID')
        name = role['name']
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name) or name.strip() in names:
            invalid('角色名称需要唯一的单行文本，长度上限 100')
        ids.add(role['id']); names.add(name.strip())
        roles.append({**originals[role['id']], 'name': name.strip(), 'permissions': permission_list(role['permissions'])})
    members = body.get('members')
    if not isinstance(members, list) or not len(members) <= 100:
        invalid('请为模板选择现有 Bot 成员')
    assigned, selected_ids = [], set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {'bot_id', 'role', 'permissions'}:
            invalid('成员需要 bot_id、一个 role 和个人 permissions')
        if type(member['bot_id']) is not int or member['bot_id'] <= 0 or member['bot_id'] in selected_ids:
            invalid('每个 Bot 在本项目选择一个角色')
        if not isinstance(member['role'], str) or member['role'] not in ids:
            invalid('成员角色必须来自模板')
        selected_ids.add(member['bot_id'])
        assigned.append({**member, 'permissions': permission_list(member['permissions'])})
    missing = [r['name'] for r in roles if r['required'] and not any(m['role'] == r['id'] for m in assigned)]
    if missing:
        invalid('必需角色尚未分配：' + '、'.join(missing))
    owner = body.get('owner_bot_id')
    if type(owner) is not int or owner not in selected_ids:
        invalid('请选择团队中的项目负责人')
    team = {'roles': roles, 'members': assigned, 'owner_bot_id': owner}
    if template.get('owner_role_id'):
        owner_members = [m['bot_id'] for m in assigned if m['role'] == template['owner_role_id']]
        if owner_members != [owner]:
            invalid('负责人角色须由唯一成员承担，并与所选负责人一致')
        team['owner_role_id'] = template['owner_role_id']
    validate_bots(conn, team)
    by_id = {r['id']: r for r in roles}
    role_ids_upper = {r['id'].upper() for r in roles}
    variables = {'PROJECT_NAME': config['display_name'], 'PROJECT_DESCRIPTION': config['description'],
                 'PROJECT_ID': project_id, 'PROJECT_ROOT': config['root'], 'DIRECTORY_MODE': config['directory_mode'],
                 'ROLE_TABLE': '\n'.join(f"- {r['name']}：{r['responsibilities']}；默认权限：{', '.join(r['permissions']) or '无'}。" for r in roles),
                 'WORKSPACE_RULES': workspace_rules(template['id'], config['directory_mode'], roles),
                 **{f'ROLE_{r["id"].upper()}': r['name'] for r in roles}}
    snapshot = {k: template[k] for k in ('id', 'version', 'name', 'description')}
    if template.get('owner_role_id'):
        snapshot['owner_role_id'] = template['owner_role_id']
    snapshot['workflow'] = render(template['workflow'], variables, role_ids_upper)
    snapshot['roles'] = roles
    snapshot['permissions'] = [{'key': k, 'label': v} for k, v in permissions.PERMISSIONS.items()]
    snapshot['references'] = [{**r, 'name': by_id[r['role_id']]['name'], 'content': render(r['content'], variables, role_ids_upper)} for r in template['references']]
    return {p: render(c, variables, role_ids_upper) for p, c in template['contents'].items()}, snapshot, team


def apply_team(conn, project, team, actor):
    """Caller owns BEGIN IMMEDIATE; every role/member/grant/owner/audit is atomic."""
    validate_bots(conn, team)
    role_ids = {r['id']: project_team.create_role(conn, project, r['name'], actor)['id'] for r in team['roles']}
    if team.get('owner_role_id'):
        project_team.save_owner_role(conn, project, role_ids[team['owner_role_id']])
    grants = {p: [] for p in permissions.PERMISSIONS}
    for role in team['roles']:
        for p in role['permissions']:
            grants[p].append(permissions._role_grant(role_ids[role['id']]))
    handles = {}
    names = {r['id']: r['name'] for r in team['roles']}
    for member in team['members']:
        handle = bot_identity.enroll(conn, member['bot_id'], project)
        handles[member['bot_id']] = handle
        ws.audit(conn, project, actor, 'member.enroll', {'profile': handle, 'bot_id': member['bot_id']})
        project_team.assign_role(conn, project, handle, names[member['role']], actor)
        for p in member['permissions']:
            grants[p].append(handle)
    permissions.save_in_transaction(conn, project, {'grants': grants, 'revision': permissions._snapshot(conn, project)['revision']}, actor)
    owner = handles[team['owner_bot_id']]
    taskpool._save_project_owner(conn, project, owner)
    result = {'role_ids': role_ids, 'member_profiles': {str(k): v for k, v in handles.items()}, 'owner_profile': owner}
    ws.audit(conn, project, actor, 'project.template.team', result)
    return result


def reference(project):
    """Resolve the project's template type, then read its current references."""
    conn = db.get_connection()
    try:
        ws.project(conn, project)
        # Current creation/import records the selected type in its atomic audit.
        row = conn.execute("SELECT detail FROM project_config_events WHERE project=? AND action='project.import' ORDER BY id DESC LIMIT 1", (project,)).fetchone()
        selected = json.loads(row['detail']).get('template') if row else None
        if row is None:
            # Earlier projects carry the same type in their initialization plan.
            for row in conn.execute("SELECT plan FROM project_initializations WHERE project=? AND phase<>'preview' ORDER BY created_at DESC", (project,)):
                selected = json.loads(row['plan']).get('template')
                if selected:
                    break
        template = None
        if selected and selected.get('id') != 'blank':
            current = load(selected['id'])
            template = {k: current[k] for k in ('id', 'name', 'version', 'description', 'references')}
        return {'project': project, 'template': template}
    finally:
        conn.close()
