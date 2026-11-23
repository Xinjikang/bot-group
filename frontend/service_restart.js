/* Restart receipts survive a page reload and the service's connection gap. */
(() => {
  const button = document.querySelector('#btn-restart');
  const key = 'ge_service_restart';
  let monitoring = false;
  const stages = {
    running: '正在重启…', draining: '正在收尾本地执行…', terminating: '正在停止本地执行…',
    releasing: '正在回收资源…', stopping: '等待服务启动…',
  };
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const read = () => { try { return JSON.parse(sessionStorage.getItem(key)); } catch { return null; } };
  const save = operation => { try { sessionStorage.setItem(key, JSON.stringify(operation)); } catch {} };
  const clear = () => { try { sessionStorage.removeItem(key); } catch {} };

  async function request(path, method = 'GET', payload) {
    const headers = S.token ? {Authorization: 'Bearer ' + S.token} : {};
    if (payload) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, {method, headers, body: JSON.stringify(payload),
      cache: 'no-store', signal: AbortSignal.timeout(method === 'POST' ? 30000 : 5000)});
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
    progress(operation ? '正在核对重启回执…' : '正在连接服务，准备重启…');
    let failure = '';
    let reloading = false;
    try {
      const deadline = Date.now() + 300000;
      while (Date.now() < deadline) {
        try {
          let receipt = await request('/admin/restart');
          if (!operation) {
            if (['starting', 'restarting'].includes(receipt.state)) {
              operation = {id: receipt.id, instance_id: receipt.instance_id};
            } else {
              const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
                n => n.toString(16).padStart(2, '0')).join('');
              operation = {id, previous_id: receipt.id || '', instance_id: receipt.current_instance_id,
                attempts: 0, retry_until: Date.now() + 60000};
            }
            save(operation);
          }
          if ('previous_id' in operation && (receipt.id || '') === operation.previous_id) {
            const keyed = !!operation.instance_id;
            if (keyed && receipt.current_instance_id !== operation.instance_id)
              throw Object.assign(new Error('服务实例已变更，请查看当前服务状态。'), {terminal: true});
            const canRetry = keyed && operation.attempts === 1 && Date.now() < operation.retry_until;
            if (operation.attempts === 0 || canRetry) {
              operation.attempts++;
              save(operation);
              progress('正在提交重启请求…');
              try {
                receipt = await request('/admin/restart', 'POST', keyed ? {
                  request_id: operation.id, instance_id: operation.instance_id, previous_id: operation.previous_id
                } : undefined);
              } catch (error) {
                if ((error.status >= 400 && error.status < 500 && ![408, 409, 429].includes(error.status)) ||
                    error.status === 503) error.terminal = true;
                throw error;
              }
            }
          }
          if ('previous_id' in operation && receipt.id && receipt.id !== operation.previous_id) {
            // Older engines allocate their own ID; keyed engines must echo ours.
            if (operation.instance_id && (operation.id !== receipt.id || operation.instance_id !== receipt.instance_id))
              throw Object.assign(new Error('服务已有其他重启回执，请查看当前服务状态。'), {terminal: true});
            operation = {id: receipt.id, instance_id: receipt.instance_id};
            save(operation);
          }
          if (receipt.id === operation.id) {
            if (receipt.state === 'failed') throw Object.assign(new Error(
              `重启失败：${receipt.error || '未知原因'}。详情见 logs/service-restart.log。`), {terminal: true});
            progress(stages[receipt.shutdown?.state] || '等待服务启动…');
            if (receipt.state === 'succeeded') {
              progress('服务已启动，正在校验实例…');
              const health = await request('/health');
              if (health.status === 'ok' && health.instance_id === receipt.new_instance_id &&
                  health.instance_id !== receipt.instance_id &&
                  health.program_root === receipt.program_root && health.db_path === receipt.db_path) {
                clear();
                reloading = true;
                progress('服务已恢复，正在刷新界面…');
                location.reload();
                return;
              }
            }
          } else if ('previous_id' in operation) {
            progress('正在等待本次重启的受理回执…');
          } else if (receipt.id) {
            throw Object.assign(new Error('重启请求已变更，请查看服务状态'), {terminal: true});
          }
        } catch (error) {
          if (error.terminal || error.status === 401 || error.status === 403) throw error;
          progress(!operation ? '正在连接服务，准备重启…' :
            'previous_id' in operation ? '正在核对重启请求是否已受理…' : '等待服务启动…');
        }
        await pause(1000);
      }
      if (!operation) throw new Error('连接服务超时，重启请求尚未提交。请检查连接后重试。');
      if ('previous_id' in operation) throw new Error('5 分钟内未取得本次重启的受理回执。刷新页面可继续核对回执。');
      throw new Error('等待重启结果超时（5 分钟）：刷新页面可继续核对回执；详情见 logs/service-restart.log。');
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
      if (!await showConfirm('重启服务？Hermes 网关中的执行会继续运行，服务启动后接回原执行。Codex 等本地执行最多等待 120 秒收尾，超时后停止。重启期间暂停新任务派发和工具写入，服务恢复后自动刷新界面；失败原因保留在回执中。')) return;
      await monitor(null);
    } catch (error) { showAlert(error.message); }
    finally { button.disabled = monitoring; }
  });

  function update() {
    button.hidden = !S.isUserIdentity || !document.querySelector('#login').hidden;
    if (!button.hidden && !monitoring) {
      const operation = read();
      if (operation) monitor(operation);
    }
  }
  document.addEventListener('service-auth-changed', update);
  update();
})();
