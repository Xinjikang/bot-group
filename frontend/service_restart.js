/* Restart receipts survive a page reload and the service's connection gap. */
(() => {
  const button = document.querySelector('#btn-restart');
  const key = 'ge_service_restart';
  let monitoring = false;
  const stages = {
    running: '正在重启…', draining: '等待 Bot 结束…', terminating: '正在停止 Bot…',
    releasing: '正在回收资源…', stopping: '等待服务启动…',
  };
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const read = () => { try { return JSON.parse(sessionStorage.getItem(key)); } catch { return null; } };
  const save = operation => { try { sessionStorage.setItem(key, JSON.stringify(operation)); } catch {} };
  const clear = () => { try { sessionStorage.removeItem(key); } catch {} };

  async function request(path, method = 'GET') {
    const headers = S.token ? {Authorization: 'Bearer ' + S.token} : {};
    const response = await fetch(path, {method, headers, cache: 'no-store', signal: AbortSignal.timeout(5000)});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.detail || `请求失败：${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function progress(message) {
    S.restartMessage = message;
    button.querySelector('.settings-label').textContent = '重启中';
    button.title = message;
    setConn('connecting', message);
  }

  async function monitor(operation) {
    if (monitoring) return;
    monitoring = true;
    S.restarting = true;
    button.disabled = true;
    S.es?.close();
    save(operation);
    progress('正在重启…');
    let failure = '';
    let reloading = false;
    try {
      const deadline = operation.deadline || Date.now() + 300000;
      while (Date.now() < deadline) {
        try {
          const receipt = await request('/admin/restart');
          if (receipt.id === operation.id) {
            if (receipt.state === 'failed') throw Object.assign(new Error(
              `重启失败：${receipt.error || '未知原因'}。详情见 logs/service-restart.log。`), {terminal: true});
            progress(stages[receipt.shutdown?.state] || '等待服务启动…');
            if (receipt.state === 'succeeded') {
              progress('服务已启动，正在校验实例…');
              const health = await request('/health');
              if (health.status === 'ok' && health.instance_id === receipt.new_instance_id &&
                  health.instance_id !== operation.instance_id &&
                  health.program_root === receipt.program_root && health.db_path === receipt.db_path) {
                clear();
                reloading = true;
                progress('服务已恢复，正在刷新界面…');
                location.reload();
                return;
              }
            }
          } else if (receipt.id) {
            throw Object.assign(new Error('重启请求已变更，请查看服务状态'), {terminal: true});
          }
        } catch (error) {
          if (error.terminal || error.status === 401 || error.status === 403) throw error;
          progress('等待服务启动…');
        }
        await pause(1000);
      }
      throw new Error('等待重启结果超时（5 分钟）：服务可能仍在启动或启动失败。刷新页面可继续核对回执；详情见 logs/service-restart.log。');
    } catch (error) {
      failure = error.message;
      if (error.terminal) clear();
    } finally {
      if (!reloading) {
        monitoring = false;
        S.restarting = false;
        S.restartMessage = '';
        button.disabled = false;
        button.querySelector('.settings-label').textContent = '重启服务';
        button.title = '重启服务';
        connectSSE();
        if (failure) showAlert(failure);
      }
    }
  }

  button.addEventListener('click', async () => {
    if (button.disabled || !S.isUserIdentity) return;
    button.disabled = true;
    try {
      if (!await showConfirm('重启服务？将暂停新任务派发，等待运行中的执行收尾（最多 120 秒）；超时后停止尚未结束的 Bot 执行并回收浏览器资源，再启动新实例。服务恢复后自动刷新界面；重启失败会保留回执，可刷新页面继续核对。')) return;
      let operation;
      try { operation = await request('/admin/restart', 'POST'); }
      catch (error) {
        // A lost POST response can still have started the restart. Reconcile
        // once through its receipt; never repeat a destructive request blindly.
        if (error.status) throw error;
        operation = await request('/admin/restart');
        if (!['starting', 'restarting'].includes(operation.state)) throw error;
      }
      operation.deadline = Date.now() + 300000;
      await monitor(operation);
    } catch (error) { showAlert(error.message); }
    finally { button.disabled = monitoring; }
  });

  function update() {
    button.hidden = !S.isUserIdentity || !document.querySelector('#login').hidden;
    if (!button.hidden && !monitoring) {
      const operation = read();
      if (operation) monitor({...operation, deadline: Date.now() + 300000});
    }
  }
  document.addEventListener('service-auth-changed', update);
  update();
})();
