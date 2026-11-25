/* Restart receipts survive a page reload and the service's connection gap. */
(() => {
  const button = document.querySelector('#btn-restart');
  const key = 'ge_service_restart';
  let monitoring = false;
  const stageText = key => window.t('restart_stage_' + (key || 'stopping'));
  const stages = {running: stageText('running'), draining: stageText('draining'), terminating: stageText('terminating'),
    releasing: stageText('releasing'), stopping: stageText('stopping')};
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
      const error = new Error(data.detail || window.t('request_failed', [response.status]));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function progress(message) {
    S.restartMessage = message;
    button.querySelector('.settings-label').textContent = window.t('restarting');
    button.title = message;
    setConn('connecting');
  }

  async function monitor(operation) {
    if (monitoring) return;
    monitoring = true;
    S.restarting = true;
    button.disabled = true;
    S.es?.close();
    progress(operation ? window.t('restart_checking_receipt') : window.t('restart_connecting'));
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
              throw Object.assign(new Error(window.t('restart_instance_changed')), {terminal: true});
            const canRetry = keyed && operation.attempts === 1 && Date.now() < operation.retry_until;
            if (operation.attempts === 0 || canRetry) {
              operation.attempts++;
              save(operation);
              progress(window.t('restart_submitting'));
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
              throw Object.assign(new Error(window.t('restart_other_receipt')), {terminal: true});
            operation = {id: receipt.id, instance_id: receipt.instance_id};
            save(operation);
          }
          if (receipt.id === operation.id) {
            if (receipt.state === 'failed') throw Object.assign(new Error(
              window.t('restart_failed', [receipt.error || window.t('unknown_reason')])), {terminal: true});
            progress(stages[receipt.shutdown?.state] || window.t('restart_waiting_start'));
            if (receipt.state === 'succeeded') {
              progress(window.t('restart_verifying'));
              const health = await request('/health');
              if (health.status === 'ok' && health.instance_id === receipt.new_instance_id &&
                  health.instance_id !== receipt.instance_id &&
                  health.program_root === receipt.program_root && health.db_path === receipt.db_path) {
                clear();
                reloading = true;
                progress(window.t('restart_refreshing'));
                location.reload();
                return;
              }
            }
          } else if ('previous_id' in operation) {
            progress(window.t('restart_waiting_receipt'));
          } else if (receipt.id) {
            throw Object.assign(new Error(window.t('restart_request_changed')), {terminal: true});
          }
        } catch (error) {
          if (error.terminal || error.status === 401 || error.status === 403) throw error;
          progress(!operation ? window.t('restart_connecting') :
            'previous_id' in operation ? window.t('restart_checking_accepted') : window.t('restart_waiting_start'));
        }
        await pause(1000);
      }
      if (!operation) throw new Error(window.t('restart_connect_timeout'));
      if ('previous_id' in operation) throw new Error(window.t('restart_no_receipt_5min'));
      throw new Error(window.t('restart_result_timeout'));
    } catch (error) {
      failure = error.message;
      if (error.terminal) clear();
    } finally {
      if (!reloading) {
        monitoring = false;
        S.restarting = false;
        S.restartMessage = '';
        button.disabled = false;
        button.querySelector('.settings-label').textContent = window.t('restart_service');
        button.title = window.t('restart_service');
        connectSSE();
        if (failure) showAlert(failure);
      }
    }
  }

  button.addEventListener('click', async () => {
    if (button.disabled || !S.isUserIdentity) return;
    button.disabled = true;
    try {
      if (!await showConfirm(window.t('restart_confirm'))) return;
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
