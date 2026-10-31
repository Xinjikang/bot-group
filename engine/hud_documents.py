"""Reviewable Markdown proposals scoped to ordinary project documentation."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile

from fastapi import HTTPException
from . import hud_inspection, project_templates


def validate(documents):
    if not isinstance(documents, list) or len(documents) > 8:
        raise ValueError('文档提案最多包含 8 个文件')
    names = set()
    for document in documents:
        if not isinstance(document, dict) or set(document) != {'path', 'content', 'sources'}:
            raise ValueError('文档提案须包含路径、完整正文和来源')
        name = document['path']
        project_templates.relative_path(name)
        path = Path(name)
        if (path.suffix != '.md' or len(path.parts) < 2 or path.parts[0] not in
                {'docs', 'doc', 'ui-spec', 'materials', 'drafts', 'outputs'} or
                any(part.startswith('.') for part in path.parts) or
                path.name.casefold() in {'agents.md', 'soul.md'} or name.casefold() in names):
            raise ValueError('文档路径须为项目资料目录内的 Markdown 文件且不能重复')
        names.add(name.casefold())
        if not isinstance(document['content'], str) or not 1 <= len(document['content']) <= 30000:
            raise ValueError('每份文档正文须为 1 至 30000 字')
        if (not isinstance(document['sources'], list) or not 1 <= len(document['sources']) <= 12 or
                any(not isinstance(s, str) or not s.strip() or len(s) > 1000 for s in document['sources'])):
            raise ValueError('文档整理须记录资料来源和确认依据')
    if sum(len(d['content']) for d in documents) > 100000:
        raise ValueError('文档提案总长度超过上限')


def target(root, name):
    base = Path(root).resolve()
    path = base / name
    if not base.is_dir() or not hud_inspection.safe_path(base, path):
        raise HTTPException(409, '文档目录不可用或包含越界链接，请重新检查')
    return path


def snapshot(root, document):
    path = target(root, document['path'])
    try:
        if not path.exists():
            return None, None
        if not path.is_file() or path.stat().st_size > 131072:
            raise HTTPException(409, '当前文档过大或路径已被占用，请使用文档编辑流程')
        raw = path.read_bytes()
        text = raw.decode('utf-8-sig')
        if len(text) > 30000:
            raise HTTPException(409, '当前文档超出预览范围，请使用文档编辑流程')
        return hashlib.sha256(raw).hexdigest(), text
    except (OSError, UnicodeError) as exc:
        raise HTTPException(409, '当前文档无法完整读取，请重新检查') from exc


def prepare(proposal, project, materials):
    documents = proposal.get('documents', [])
    if documents and not project.get('name'):
        raise ValueError('项目创建后才能保存文档整理提案')
    if documents and any(k in proposal['changes'] for k in ('root', 'directory_mode', 'worktree_root')):
        raise ValueError('请分别确认目录配置和文档整理')
    if not documents:
        return
    proposal['document_root'] = str(Path(project['root']).resolve())
    for document in documents:
        digest, before = snapshot(project['root'], document)
        if digest is not None:
            source = materials['documents'].get(document['path'])
            if not source or source['truncated'] or source['sha256'] != digest:
                raise ValueError('文档尚未完整读取或已经变化，请重新检查后整理')
        document.update(before_sha256=digest, before=before)
    proposal['documents'] = [d for d in documents
                             if (d['before'] or '').replace('\r\n', '\n') != d['content'].replace('\r\n', '\n')]


def replace(path, raw):
    handle, name = tempfile.mkstemp(prefix='.hud-', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(raw)
        if path.exists():
            os.chmod(name, path.stat().st_mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def apply(proposal, project):
    documents = proposal.get('documents', [])
    originals, written, created = [], [], []
    if documents and proposal.get('document_root') != str(Path(project['root']).resolve()):
        raise HTTPException(409, '项目目录已改变，请重新生成文档提案')
    for document in documents:
        digest, _ = snapshot(project['root'], document)
        if digest != document.get('before_sha256'):
            raise HTTPException(409, '文档已被修改，请重新检查；当前提案保留')
        path = target(project['root'], document['path'])
        originals.append((path, path.read_bytes() if path.exists() else None, document))
    try:
        for path, raw, document in originals:
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for folder in reversed(missing):
                folder.mkdir()
                created.append(folder)
            target(project['root'], document['path'])
            replace(path, document['content'].encode('utf-8'))
            written.append((path, raw))
        yield
    except BaseException:
        for path, raw in reversed(written):
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                replace(path, raw)
        for folder in reversed(created):
            folder.rmdir()
        raise
