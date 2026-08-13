const API = '';

let expandedInstanceId = null;
let activeCallRow = null;

function setServerStatus(ok) {
  const status = document.getElementById('server-status');
  status.textContent = ok ? '● 服务已连接' : '● 连接失败';
  status.style.color = ok ? '#86efac' : '#fca5a5';
}

function showAlert(type, message) {
  const alert = document.getElementById('test-alert');
  alert.className = `alert ${type} show`;
  alert.textContent = message;
}

function clearAlert() {
  document.getElementById('test-alert').className = 'alert';
}

function statusBadge(status) {
  const badge = document.createElement('span');
  const safeStatus = String(status || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '');
  badge.className = `badge-status s-${safeStatus}`;
  badge.textContent = status || 'unknown';
  return badge;
}

function parseValue(value) {
  const trimmed = value.trim();
  if (!trimmed) return '';
  try {
    return JSON.parse(trimmed);
  } catch (_) {
    return value;
  }
}

function collectKeyValues(editor) {
  const result = {};
  editor.querySelectorAll('.kv-row').forEach(row => {
    const key = row.querySelector('.kv-key').value.trim();
    if (!key) return;
    result[key] = parseValue(row.querySelector('.kv-value').value);
  });
  return result;
}

function addKeyValueRow(editor, key = '', value = '') {
  const row = document.createElement('div');
  row.className = 'kv-row';

  const keyInput = document.createElement('input');
  keyInput.className = 'kv-key';
  keyInput.type = 'text';
  keyInput.placeholder = '键名';
  keyInput.value = key;
  keyInput.setAttribute('aria-label', '键名');

  const valueInput = document.createElement('input');
  valueInput.className = 'kv-value';
  valueInput.type = 'text';
  valueInput.placeholder = '值，例如 true、42、[1, 2]';
  valueInput.value = value;
  valueInput.setAttribute('aria-label', '值');

  const remove = document.createElement('button');
  remove.className = 'btn btn-ghost btn-sm';
  remove.type = 'button';
  remove.textContent = '删除';
  remove.addEventListener('click', () => row.remove());

  row.append(keyInput, valueInput, remove);
  editor.querySelector('.kv-rows').appendChild(row);
}

function createKeyValueEditor(labelText) {
  const group = document.createElement('div');
  group.className = 'form-group';

  const label = document.createElement('label');
  label.textContent = labelText;

  const editor = document.createElement('div');
  editor.className = 'kv-editor';
  const header = document.createElement('div');
  header.className = 'kv-editor-header';
  header.innerHTML = '<span>键</span><span>值</span><span>操作</span>';
  const rows = document.createElement('div');
  rows.className = 'kv-rows';
  const add = document.createElement('button');
  add.className = 'btn btn-ghost btn-sm kv-add';
  add.type = 'button';
  add.textContent = '添加键值对';
  add.addEventListener('click', () => addKeyValueRow(editor));

  editor.append(header, rows, add);
  group.append(label, editor);
  addKeyValueRow(editor);
  return group;
}

function closeCallPanel() {
  if (activeCallRow) activeCallRow.remove();
  activeCallRow = null;
  expandedInstanceId = null;
  document.querySelectorAll('.instance-call-button').forEach(button => {
    button.textContent = '调用';
    button.setAttribute('aria-expanded', 'false');
  });
}

function formatCallResult(data) {
  return [
    `状态: ${data.status || 'unknown'}`,
    `Task ID: ${data.task_id || '—'}`,
    '',
    '结果:',
    JSON.stringify(data.result, null, 2) ?? 'null',
    '',
    '错误:',
    data.error_message || '无',
    '',
    'Metadata:',
    JSON.stringify(data.metadata || {}, null, 2),
  ].join('\n');
}

function openCallPanel(instance, parentRow, trigger) {
  if (expandedInstanceId === instance.instance_id) {
    closeCallPanel();
    return;
  }
  closeCallPanel();
  expandedInstanceId = instance.instance_id;
  trigger.textContent = '收起';
  trigger.setAttribute('aria-expanded', 'true');

  const row = document.createElement('tr');
  row.className = 'test-call-row';
  const cell = document.createElement('td');
  cell.colSpan = 4;
  const panel = document.createElement('div');
  panel.className = 'test-call-panel';
  const form = document.createElement('form');

  const grid = document.createElement('div');
  grid.className = 'test-call-grid';
  const taskGroup = document.createElement('div');
  taskGroup.className = 'form-group task-field';
  const taskLabel = document.createElement('label');
  taskLabel.textContent = '任务描述 *';
  taskLabel.htmlFor = `task-description-${instance.instance_id}`;
  const taskDescription = document.createElement('textarea');
  taskDescription.id = taskLabel.htmlFor;
  taskDescription.required = true;
  taskDescription.placeholder = '请输入希望智能体执行的任务';
  taskGroup.append(taskLabel, taskDescription);

  const parametersGroup = createKeyValueEditor('参数');
  const metadataGroup = createKeyValueEditor('Metadata');
  grid.append(taskGroup, parametersGroup, metadataGroup);

  const actions = document.createElement('div');
  actions.className = 'test-call-actions';
  const submit = document.createElement('button');
  submit.className = 'btn btn-primary';
  submit.type = 'submit';
  submit.textContent = '确认调用';
  const callState = document.createElement('span');
  callState.className = 'test-call-state';
  actions.append(submit, callState);

  const result = document.createElement('pre');
  result.className = 'test-result';
  result.setAttribute('aria-live', 'polite');

  form.append(grid, actions, result);
  panel.appendChild(form);
  cell.appendChild(panel);
  row.appendChild(cell);
  parentRow.after(row);
  activeCallRow = row;
  taskDescription.focus();

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const description = taskDescription.value.trim();
    if (!description) {
      taskDescription.focus();
      return;
    }

    submit.disabled = true;
    submit.textContent = '调用中...';
    callState.textContent = '正在等待智能体响应';
    result.classList.remove('show');
    result.textContent = '';

    try {
      const response = await fetch(`${API}/tests/call`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          instance_id: instance.instance_id,
          task_description: description,
          parameters: collectKeyValues(parametersGroup.querySelector('.kv-editor')),
          metadata: collectKeyValues(metadataGroup.querySelector('.kv-editor')),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      callState.textContent = data.status === 'success' ? '调用成功' : '调用已完成';
      result.textContent = formatCallResult(data);
      result.classList.add('show');
    } catch (error) {
      callState.textContent = '调用失败';
      result.textContent = `请求失败:\n${error.message}`;
      result.classList.add('show');
    } finally {
      submit.disabled = false;
      submit.textContent = '确认调用';
    }
  });
}

function renderInstances(instances) {
  closeCallPanel();
  const body = document.getElementById('instances-body');
  body.replaceChildren();
  if (!instances.length) {
    const row = document.createElement('tr');
    row.className = 'empty-row';
    const cell = document.createElement('td');
    cell.colSpan = 4;
    cell.textContent = '暂无 Agent 实例';
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  instances.forEach(instance => {
    const row = document.createElement('tr');
    const name = document.createElement('td');
    name.textContent = instance.agent_id || '—';
    const status = document.createElement('td');
    status.appendChild(statusBadge(instance.status));
    const instanceId = document.createElement('td');
    instanceId.className = 'test-instance-id';
    instanceId.textContent = instance.instance_id || '—';
    const operation = document.createElement('td');
    const call = document.createElement('button');
    call.className = 'btn btn-primary btn-sm instance-call-button';
    call.type = 'button';
    call.textContent = '调用';
    call.disabled = instance.status !== 'running';
    call.setAttribute('aria-expanded', 'false');
    if (call.disabled) call.title = '仅运行中的实例可以调用';
    call.addEventListener('click', () => openCallPanel(instance, row, call));
    const stop = document.createElement('button');
    stop.className = 'btn btn-danger btn-sm instance-stop-button';
    stop.type = 'button';
    stop.textContent = '停止';
    stop.disabled = !['running', 'error'].includes(instance.status);
    if (stop.disabled) stop.title = '该实例已停止或正在停止';
    stop.addEventListener('click', async () => {
      if (!window.confirm(`确定停止实例 ${instance.instance_id}？对应 Pod 和 Service 将被删除。`)) return;
      closeCallPanel();
      stop.disabled = true;
      call.disabled = true;
      stop.textContent = '停止中...';
      clearAlert();
      try {
        const response = await fetch(
          `${API}/api/agents/instances/${encodeURIComponent(instance.instance_id)}`,
          {method: 'DELETE'},
        );
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        await loadInstances();
        showAlert('success', `实例 ${instance.instance_id} 已停止，Pod 和 Service 已删除。`);
      } catch (error) {
        showAlert('error', `停止 Agent 实例失败：${error.message}`);
        stop.disabled = false;
        call.disabled = instance.status !== 'running';
        stop.textContent = '停止';
      }
    });
    operation.className = 'test-instance-actions';
    operation.append(call, stop);
    row.append(name, status, instanceId, operation);
    body.appendChild(row);
  });
}

async function loadInstances() {
  const refresh = document.getElementById('refresh-instances');
  refresh.disabled = true;
  clearAlert();
  try {
    const response = await fetch(`${API}/api/agents/instances`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    renderInstances(data.instances || []);
    document.getElementById('instances-refresh-hint').textContent =
      `上次更新 ${new Date().toLocaleTimeString('zh-CN')}`;
    setServerStatus(true);
  } catch (error) {
    renderInstances([]);
    showAlert('error', `加载 Agent 实例失败：${error.message}`);
    setServerStatus(false);
  } finally {
    refresh.disabled = false;
  }
}

document.getElementById('refresh-instances').addEventListener('click', loadInstances);
loadInstances();
