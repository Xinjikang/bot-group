// Removed projects retain data and local files, and stay hidden from the UI.
window.removeProjectRegistration = async name => {
  if (!await showConfirm('移除这个项目？项目配置、聊天和任务历史及本地文件都会保留。目录释放后可以重新导入。')) return false;
  try {
    await api(`/projects/${encodeURIComponent(name)}/remove`, {method:'POST'});
    if (S.currentProject === name) selectProject(null);
    await loadProjects(); return true;
  } catch (err) { showAlert('移除失败：' + err.message); return false; }
};
