// Removed projects retain data and local files, and stay hidden from the UI.
window.removeProjectRegistration = async name => {
  if (!await showConfirm(window.t('pr_confirm'))) return false;
  try {
    await api(`/projects/${encodeURIComponent(name)}/remove`, {method:'POST'});
    if (S.currentProject === name) selectProject(null);
    await loadProjects(); return true;
  } catch (err) { showAlert(window.t('pr_failed', [err.message])); return false; }
};
