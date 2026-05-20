// ====================================================================
// Utilities
// ====================================================================
const API = '';  // same origin

function showAlert(id, type, msg, duration=4000) {
  const el = document.getElementById(id);
  el.className = `alert ${type} show`;
  el.textContent = msg;
  if (duration > 0) setTimeout(() => el.classList.remove('show'), duration);
}

function statusBadge(s) {
  return `<span class="badge-status s-${s}">${s}</span>`;
}

function fmtTime() {
  return new Date().toLocaleTimeString('zh-CN');
}

// ====================================================================
// Tab switching
// ====================================================================
const TAB_NAMES = ['apps','instances','qos','resources','nats'];
const TAB_LOADERS = { apps: loadApps, instances: loadInstances, qos: loadQos, resources: loadResources, nats: loadNatsTab };
let activeTab = 'apps';

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => {
    b.classList.toggle('active', TAB_NAMES[i] === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  activeTab = name;
  TAB_LOADERS[name]();
}

// ====================================================================
// Collapse
// ====================================================================
function toggleCollapse(header) {
  header.classList.toggle('open');
  header.nextElementSibling.classList.toggle('open');
}

// ====================================================================
// Tab: APPS
// ====================================================================
let warehouseImages = [];

// 记住哪些查询面板是展开的，以及面板里的输入内容和结果
const _openPanels = new Set();       // app_id → 查询面板 open
const _panelInput = {};              // app_id → 查询输入框内容
const _panelResult = {};             // app_id → 查询结果
const _pendingQueries = new Set();   // app_id → 查询请求进行中
const _openStartPanels = new Set();  // app_id → 启动面板 open
// 编辑面板状态
const _openEditPanels = new Set();   // app_id → 编辑面板 open
const _editName = {};                // app_id → 名称输入内容
const _editSkills = {};              // app_id → skills 输入内容
let _lastAppsSignature = null;       // 上次成功渲染的 apps 结果签名

function _saveQueryState() {
  // 保存查询面板状态
  document.querySelectorAll('[id^="qp-"]').forEach(el => {
    const appId = el.id.slice(3);
    if (el.classList.contains('open')) {
      _openPanels.add(appId);
      const inp = document.getElementById('qi-' + appId);
      if (inp) _panelInput[appId] = inp.value;
      const res = document.getElementById('qr-' + appId);
      if (res) _panelResult[appId] = { text: res.textContent, show: res.classList.contains('show') };
    } else {
      _openPanels.delete(appId);
    }
  });
  // 保存调度历史面板状态
  document.querySelectorAll('[id^="shp-"]').forEach(el => {
    const appId = el.id.slice(4);
    if (el.classList.contains('open')) _openHistoryPanels.add(appId);
    else _openHistoryPanels.delete(appId);
  });
  // 保存编辑面板状态
  document.querySelectorAll('[id^="ep-"]').forEach(el => {
    const appId = el.id.slice(3);
    if (el.classList.contains('open')) {
      _openEditPanels.add(appId);
      const nameEl = document.getElementById('en-' + appId);
      if (nameEl) _editName[appId] = nameEl.value;
      const skillsEl = document.getElementById('es-' + appId);
      if (skillsEl) _editSkills[appId] = skillsEl.value;
    } else {
      _openEditPanels.delete(appId);
    }
  });
  // 保存启动面板状态
  document.querySelectorAll('[id^="sp-"]').forEach(el => {
    const appId = el.id.slice(3);
    if (el.classList.contains('open')) _openStartPanels.add(appId);
    else _openStartPanels.delete(appId);
  });
}

function _restoreQueryState() {
  // 恢复查询面板
  _openPanels.forEach(appId => {
    const panel = document.getElementById('qp-' + appId);
    if (panel) {
      panel.classList.add('open');
      const inp = document.getElementById('qi-' + appId);
      if (inp && _panelInput[appId] != null) inp.value = _panelInput[appId];
      const res = document.getElementById('qr-' + appId);
      if (res && _panelResult[appId]) {
        res.textContent = _panelResult[appId].text;
        if (_panelResult[appId].show) res.classList.add('show');
      }
    }
  });
  // 恢复调度历史面板
  _openHistoryPanels.forEach(appId => {
    const panel = document.getElementById('shp-' + appId);
    if (panel) panel.classList.add('open');
  });
  // 恢复编辑面板
  _openEditPanels.forEach(appId => {
    const panel = document.getElementById('ep-' + appId);
    if (panel) {
      panel.classList.add('open');
      const nameEl = document.getElementById('en-' + appId);
      if (nameEl && _editName[appId] != null) nameEl.value = _editName[appId];
      const skillsEl = document.getElementById('es-' + appId);
      if (skillsEl && _editSkills[appId] != null) skillsEl.value = _editSkills[appId];
    }
  });
  _openStartPanels.forEach(appId => {
    const panel = document.getElementById('sp-' + appId);
    if (panel) panel.classList.add('open');
  });
}

async function loadApps() {
  // 如果有查询面板正在展开，先保存状态
  _saveQueryState();
  // 查询请求执行期间暂停自动刷新，避免重绘后结果写回旧 DOM 节点
  if (_pendingQueries.size > 0) {
    document.getElementById('apps-refresh-hint').textContent = `(查询执行中，刷新暂停) ${fmtTime()}`;
    return;
  }
  // 如果有面板展开且用户正在交互（输入框有焦点），跳过本次刷新避免打断
  const _activeId = document.activeElement && document.activeElement.id;
  if (_activeId && (_activeId.startsWith('qi-') || _activeId.startsWith('en-') || _activeId.startsWith('es-'))) {
    document.getElementById('apps-refresh-hint').textContent = `(编辑中，刷新暂停) ${fmtTime()}`;
    return;
  }
  if (_openEditPanels.size > 0) {
    document.getElementById('apps-refresh-hint').textContent = `(编辑面板开启，刷新暂停) ${fmtTime()}`;
    return;
  }
  try {
    const res = await fetch(`${API}/api/apps/`);
    const data = await res.json();
    const apps = data.apps || [];
    const signature = JSON.stringify(apps);
    if (signature === _lastAppsSignature) {
      document.getElementById('apps-refresh-hint').textContent = `数据无变化，未刷新 ${fmtTime()}`;
      return;
    }
    _lastAppsSignature = signature;
    renderApps(apps);
    _restoreQueryState();
    document.getElementById('apps-refresh-hint').textContent = `上次更新 ${fmtTime()}`;
  } catch(e) {
    document.getElementById('apps-tbody').innerHTML =
      `<tr class="empty-row"><td colspan="5">❌ 加载失败: ${e.message}</td></tr>`;
  }
}

async function loadWarehouseImages() {
  try {
    const res = await fetch(`${API}/api/warehouse/images`);
    const data = await res.json();
    warehouseImages = dedupeWarehouseImages(data.images || []);
    renderAgentChoices(warehouseImages);
  } catch(e) {
    warehouseImages = [];
    renderAgentChoices([]);
  }
}

function dedupeWarehouseImages(images) {
  const preferred = ['agent-grpc', 'agent-b', 'agent-c'];
  const byCap = new Map();
  for (const img of images) {
    if (!img || !img.capability) continue;
    if (!preferred.includes(img.capability)) continue;
    if (!byCap.has(img.capability)) byCap.set(img.capability, img);
  }
  return preferred.filter(cap => byCap.has(cap)).map(cap => byCap.get(cap));
}

function renderAgentChoices(images) {
  const host = document.getElementById('f-agent-choices');
  const fallback = [
    { capability: 'agent-grpc', name: 'agent_gRPC', description: 'gRPC 入口，接收远程请求并发布到 NATS' },
    { capability: 'agent-b', name: 'agent-b', description: 'NATS worker，转发到 Agent C 并回传结果' },
    { capability: 'agent-c', name: 'agent-c', description: 'NATS worker，处理消息并返回转换结果' },
  ];
  const list = images.length ? images : fallback;
  host.innerHTML = list.map(img => `
    <label class="choice-item">
      <input type="checkbox" value="${escHtml(img.capability)}">
      <div>
        <strong>${escHtml((img.name || img.capability).replace('agent-', 'Agent '))}</strong>
        <span>${escHtml(img.description || img.image_id || img.capability)}</span>
      </div>
    </label>
  `).join('');
}

function renderApps(apps) {
  const tbody = document.getElementById('apps-tbody');
  if (!apps.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">暂无应用，请先安装</td></tr>';
    return;
  }
  const _hasScheduleConfig = (a) => a.guidance_file && a.guidance_file.constraints && a.guidance_file.constraints.schedule_interval_seconds > 0;
  tbody.innerHTML = apps.map(a => {
    const hasSched = _hasScheduleConfig(a);
    const schedInterval = hasSched ? a.guidance_file.constraints.schedule_interval_seconds : 0;
    return `
    <tr id="app-row-${a.app_id}">
      <td><strong>${escHtml(a.name)}</strong></td>
      <td style="font-family:monospace;font-size:12px;color:#778">${a.app_id}</td>
      <td>${statusBadge(a.status)}${a.status==='scheduled'?`<div style="font-size:11px;color:#7c3aed;margin-top:2px">每 ${schedInterval}s</div>`:''}</td>
      <td style="font-family:monospace;font-size:12px;color:#aaa">${a.workflow_handle||'—'}</td>
      <td>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${a.status==='idle'||a.status==='stopped'
            // ? `<button class="btn btn-success btn-sm" onclick="toggleStart('${a.app_id}')">▶ 启动</button>`
            ? `<button class="btn btn-success btn-sm" onclick="startApp('${a.app_id}')">▶ 启动</button>`
            : ''}
          ${(a.status==='idle'||a.status==='stopped') && hasSched
            ? `<button class="btn btn-sm" style="background:#7c3aed;color:#fff" onclick="startSchedule('${a.app_id}')">⏱ 定时启动</button>`
            : ''}
          ${a.status==='scheduled'
            ? `<button class="btn btn-danger btn-sm" onclick="stopSchedule('${a.app_id}')">⏹ 停止调度</button>
               <button class="btn btn-ghost btn-sm" onclick="toggleScheduleHistory('${a.app_id}')">📋 历史</button>`
            : ''}
          ${a.status==='running'
            ? `<button class="btn btn-warning btn-sm" onclick="toggleQuery('${a.app_id}')">💬 查询</button>
               <button class="btn btn-danger btn-sm" onclick="stopApp('${a.app_id}')">⏹ 停止</button>`
            : ''}
          ${hasSched && (a.status==='idle'||a.status==='stopped')
            ? `<button class="btn btn-ghost btn-sm" onclick="toggleScheduleHistory('${a.app_id}')">📋 历史</button>`
            : ''}
          <button class="btn btn-ghost btn-sm" onclick="toggleEdit('${a.app_id}')">✏️</button>
          <button class="btn btn-ghost btn-sm" onclick="uninstallApp('${a.app_id}')">🗑</button>
          <button class="btn btn-ghost btn-sm" onclick="openAppDetails('${a.app_id}')">📄 应用详情</button>
        </div>
        <!-- 编辑面板 -->
        <div class="query-panel" id="sp-${a.app_id}">
          <div style="display:flex;flex-direction:column;gap:10px;padding-top:4px">
            <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#445">
              <input id="sa-${a.app_id}" type="checkbox" checked>
              自动分配资源
            </label>
            <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px">
              <div>
                <label style="font-size:12px;color:#778">CPU</label>
                <input id="sc-${a.app_id}" type="number" min="0.1" step="0.1" placeholder="自动" style="width:100%;border:1px solid #d0d6e0;border-radius:6px;padding:6px 8px;font-size:13px">
              </div>
              <div>
                <label style="font-size:12px;color:#778">内存 MB</label>
                <input id="sm-${a.app_id}" type="number" min="128" step="128" placeholder="自动" style="width:100%;border:1px solid #d0d6e0;border-radius:6px;padding:6px 8px;font-size:13px">
              </div>
              <div>
                <label style="font-size:12px;color:#778">GPU</label>
                <input id="sg-${a.app_id}" type="number" min="0" step="1" placeholder="自动" style="width:100%;border:1px solid #d0d6e0;border-radius:6px;padding:6px 8px;font-size:13px">
              </div>
              <div>
                <label style="font-size:12px;color:#778">节点</label>
                <input id="sn-${a.app_id}" type="text" placeholder="自动" style="width:100%;border:1px solid #d0d6e0;border-radius:6px;padding:6px 8px;font-size:13px">
              </div>
            </div>
            <div style="display:flex;gap:8px;justify-content:flex-end">
              <button class="btn btn-ghost btn-sm" onclick="toggleStart('${a.app_id}')">取消</button>
              <button class="btn btn-success btn-sm" onclick="startApp('${a.app_id}')">确认启动</button>
            </div>
          </div>
        </div>
        <!-- 编辑面板 -->
        <div class="query-panel" id="ep-${a.app_id}">
          <div style="display:flex;flex-direction:column;gap:8px;padding-top:8px">
            <div style="display:flex;gap:8px">
              <div style="flex:1">
                <label style="font-size:12px;color:#778">应用名称</label>
                <input id="en-${a.app_id}" type="text" value="${escHtml(a.name)}" style="width:100%;border:1px solid #d0d6e0;border-radius:6px;padding:6px 8px;font-size:13px">
              </div>
            </div>
            <div>
              <label style="font-size:12px;color:#778">Skills.md 内容</label>
              <textarea id="es-${a.app_id}" style="width:100%;border:1px solid #d0d6e0;border-radius:6px;padding:6px 8px;font-size:12px;font-family:monospace;resize:vertical;min-height:80px" placeholder="# 应用技能&#10;## Pipeline&#10;search:描述 -> nlp:描述">${escHtml((a.guidance_file && a.guidance_file.skills_content) || '')}</textarea>
            </div>
            <div style="display:flex;gap:8px;justify-content:flex-end">
              <button class="btn btn-ghost btn-sm" onclick="toggleEdit('${a.app_id}')">取消</button>
              <button class="btn btn-primary btn-sm" onclick="updateApp('${a.app_id}')">保存</button>
            </div>
          </div>
        </div>
        <!-- 查询面板 -->
        <div class="query-panel" id="qp-${a.app_id}">
          <div style="display:flex;gap:8px;align-items:flex-start">
            <textarea id="qi-${a.app_id}" style="flex:1;border:1px solid #d0d6e0;border-radius:6px;padding:8px;font-size:13px;resize:vertical;min-height:60px" placeholder="输入查询内容..."></textarea>
            <button class="btn btn-primary btn-sm" id="qbtn-${a.app_id}" onclick="sendQuery('${a.app_id}')">发送</button>
          </div>
          <div class="query-result" id="qr-${a.app_id}"></div>
        </div>
        <!-- 调度历史面板 -->
        <div class="query-panel" id="shp-${a.app_id}">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <strong style="font-size:13px">调度执行历史</strong>
            <div style="display:flex;gap:6px;align-items:center">
              <span id="sh-status-${a.app_id}" style="font-size:11px;color:#778"></span>
              <button class="btn btn-ghost btn-sm" onclick="loadScheduleHistory('${a.app_id}')">🔄</button>
            </div>
          </div>
          <div id="sh-body-${a.app_id}"><div style="color:#aaa;font-size:12px">加载中...</div></div>
        </div>
      </td>
    </tr>
  `}).join('');
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function installApp() {
  const name = document.getElementById('f-name').value.trim();
  const task = document.getElementById('f-task').value.trim();
  if (!name || !task) { showAlert('apps-alert','error','请填写应用名称和任务描述'); return; }
  const selected = [...document.querySelectorAll('#f-agent-choices input[type="checkbox"]:checked')].map(el => el.value);
  const agents = selected;
  const skillsMd = document.getElementById('f-skills').value.trim() || null;
  const schedInterval = parseInt(document.getElementById('f-schedule-interval').value)||0;
  const schedParallel = parseInt(document.getElementById('f-schedule-parallel').value)||5;
  const schedAutoRestart = document.getElementById('f-schedule-autorestart').checked;
  const constraints = { timeout_seconds: parseInt(document.getElementById('f-timeout').value)||120 };
  if (schedInterval > 0) {
    constraints.schedule_interval_seconds = schedInterval;
    constraints.schedule_max_parallel = schedParallel;
    constraints.schedule_auto_restart = schedAutoRestart;
  }
  const images = warehouseImages
    .filter(img => selected.includes(img.capability))
    .map(img => ({
      image_id: img.image_id,
      name: img.name,
      version: img.version,
      capability: img.capability,
      description: img.description,
      exposed_external: !!img.exposed_external,
      metadata: img.metadata || {}
    }));
  const body = {
    name,
    task_description: task,
    orchestration_mode: document.getElementById('f-mode').value,
    agents_required: agents,
    images,
    constraints: constraints,
    skills_md: skillsMd
  };
  try {
    const res = await fetch(`${API}/api/apps/install`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail||JSON.stringify(data));
    showAlert('apps-alert','success',`✅ 安装成功：${data.name}（${data.app_id}）`);
    document.getElementById('f-name').value='';
    document.getElementById('f-task').value='';
    document.querySelectorAll('#f-agent-choices input[type="checkbox"]').forEach(el => { el.checked = false; });
    loadApps();
  } catch(e) {
    showAlert('apps-alert','error',`❌ 安装失败：${e.message}`);
  }
}

async function startApp(appId) {
  try {
    const auto = document.getElementById(`sa-${appId}`)?.checked ?? true;
    let body = {};
    if (!auto) {
      const cpu = document.getElementById(`sc-${appId}`)?.value.trim();
      const memory = document.getElementById(`sm-${appId}`)?.value.trim();
      const gpu = document.getElementById(`sg-${appId}`)?.value.trim();
      const node = document.getElementById(`sn-${appId}`)?.value.trim();
      const rc = {};
      if (cpu) rc.cpu_cores = parseFloat(cpu);
      if (memory) rc.memory_mb = parseInt(memory, 10);
      if (gpu !== '') rc.gpu_count = parseInt(gpu, 10);
      if (node) rc.node_id = node;
      if (Object.keys(rc).length) body.resource_config = rc;
    }
    const res = await fetch(`${API}/api/apps/${appId}/start`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail||JSON.stringify(data));
    showAlert('apps-alert','success',`▶ 启动成功：${data.workflow_handle}`);
    document.getElementById(`sp-${appId}`)?.classList.remove('open');
    _openStartPanels.delete(appId);
    loadApps();
  } catch(e) {
    showAlert('apps-alert','error',`❌ 启动失败：${e.message}`);
  }
}

function toggleStart(appId) {
  const panel = document.getElementById(`sp-${appId}`);
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) _openStartPanels.add(appId);
  else _openStartPanels.delete(appId);
}

async function stopApp(appId) {
  try {
    const res = await fetch(`${API}/api/apps/${appId}/stop`, {method:'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail||JSON.stringify(data));
    showAlert('apps-alert','success','⏹ 应用已停止');
    loadApps();
  } catch(e) {
    showAlert('apps-alert','error',`❌ 停止失败：${e.message}`);
  }
}

async function uninstallApp(appId) {
  if (!confirm(`确认卸载应用 ${appId}？`)) return;
  try {
    const res = await fetch(`${API}/api/apps/${appId}`, {method:'DELETE'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail||JSON.stringify(data));
    showAlert('apps-alert','success','🗑 卸载成功');
    loadApps();
  } catch(e) {
    showAlert('apps-alert','error',`❌ 卸载失败：${e.message}`);
  }
}

function toggleQuery(appId) {
  const panel = document.getElementById(`qp-${appId}`);
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) {
    _openPanels.add(appId);
    // 自动聚焦输入框
    const inp = document.getElementById('qi-' + appId);
    if (inp) setTimeout(() => inp.focus(), 50);
  } else {
    _openPanels.delete(appId);
  }
}

function toggleEdit(appId) {
  const panel = document.getElementById(`ep-${appId}`);
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) {
    _openEditPanels.add(appId);
    // 初始化缓存（避免首次进入覆盖 API 值）
    const nameEl = document.getElementById('en-' + appId);
    if (nameEl && _editName[appId] == null) _editName[appId] = nameEl.value;
    const skillsEl = document.getElementById('es-' + appId);
    if (skillsEl && _editSkills[appId] == null) _editSkills[appId] = skillsEl.value;
    setTimeout(() => { if (nameEl) nameEl.focus(); }, 50);
  } else {
    _openEditPanels.delete(appId);
    // 关闭时清除缓存，下次重新从 API 数据填充
    delete _editName[appId];
    delete _editSkills[appId];
  }
}

async function updateApp(appId) {
  const name = document.getElementById(`en-${appId}`).value.trim() || null;
  const skillsMd = document.getElementById(`es-${appId}`).value.trim() || null;
  try {
    const res = await fetch(`${API}/api/apps/${appId}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name, skills_md: skillsMd }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
    showAlert('apps-alert', 'success', `✅ 更新成功：${appId}`);
    document.getElementById(`ep-${appId}`).classList.remove('open');
    loadApps();
  } catch(e) {
    showAlert('apps-alert', 'error', `❌ 更新失败：${e.message}`);
  }
}

async function sendQuery(appId) {
  const input = document.getElementById(`qi-${appId}`);
  const query = input.value.trim();
  if (!query) return;
  const btn = document.getElementById(`qbtn-${appId}`);
  const resultEl = document.getElementById(`qr-${appId}`);
  _pendingQueries.add(appId);
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  resultEl.className = 'query-result show';
  resultEl.textContent = '⏳ 执行中，请稍候...';
  _panelResult[appId] = { text: resultEl.textContent, show: true };
  try {
    const res = await fetch(`${API}/api/apps/${appId}/interface`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({query})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail||JSON.stringify(data));
    let text;
    if (typeof data.result === 'string') {
      text = data.result;
    } else if (data.result && Array.isArray(data.result.messages) && data.result.messages.length > 0) {
      // 从后往前找第一条非用户消息（type 不是 human，role 不是 user）
      const msgs = data.result.messages;
      const aiMsg = [...msgs].reverse().find(m =>
        m.type !== 'human' && m.role !== 'user'
      ) || msgs[msgs.length - 1]; // fallback：直接取最后一条
      const c = aiMsg.content;
      text = typeof c === 'string' ? c : JSON.stringify(c, null, 2);
    } else {
      text = JSON.stringify(data, null, 2);
    }
    resultEl.textContent = text;
    _panelResult[appId] = { text, show: true };
  } catch(e) {
    const text = `❌ 错误：${e.message}`;
    resultEl.textContent = text;
    _panelResult[appId] = { text, show: true };
  } finally {
    _pendingQueries.delete(appId);
    btn.disabled = false;
    btn.innerHTML = '发送';
  }
}

// 打开应用详情页（前端路由到服务器渲染的详情模板）
function openAppDetails(appId) {
  // 使用 ui 的模板路由，后端路由在 src/api/app.py 已添加
  window.location.href = `/ui/apps/${encodeURIComponent(appId)}`;
}

// ====================================================================
// Schedule controls
// ====================================================================
async function startSchedule(appId) {
  try {
    const res = await fetch(`${API}/api/apps/${appId}/schedule/start`, {method:'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail||JSON.stringify(data));
    showAlert('apps-alert','success','⏱ 周期调度已启动');
    loadApps();
  } catch(e) {
    showAlert('apps-alert','error',`❌ 启动调度失败：${e.message}`);
  }
}

async function stopSchedule(appId) {
  try {
    const res = await fetch(`${API}/api/apps/${appId}/schedule/stop`, {method:'POST'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail||JSON.stringify(data));
    showAlert('apps-alert','success','⏹ 周期调度已停止');
    loadApps();
  } catch(e) {
    showAlert('apps-alert','error',`❌ 停止调度失败：${e.message}`);
  }
}

const _openHistoryPanels = new Set();

function toggleScheduleHistory(appId) {
  const panel = document.getElementById(`shp-${appId}`);
  if (!panel) return;
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) {
    _openHistoryPanels.add(appId);
    loadScheduleHistory(appId);
  } else {
    _openHistoryPanels.delete(appId);
  }
}

function schedBadge(s) {
  return `<span class="sched-badge sb-${s}">${s}</span>`;
}

async function loadScheduleHistory(appId) {
  const body = document.getElementById(`sh-body-${appId}`);
  const statusEl = document.getElementById(`sh-status-${appId}`);
  if (!body) return;
  try {
    // Fetch status and history in parallel
    const [statusRes, histRes] = await Promise.all([
      fetch(`${API}/api/apps/${appId}/schedule/status`),
      fetch(`${API}/api/apps/${appId}/schedule/history?limit=20`)
    ]);
    const statusData = await statusRes.json();
    const histData = await histRes.json();

    // Show status summary
    if (statusData.scheduled) {
      statusEl.innerHTML = `<span style="color:#7c3aed">活跃 ${statusData.active_runs||0} 个 / 累计 ${statusData.total_runs||0} 次</span>`;
    } else {
      statusEl.textContent = '未调度';
    }

    const records = histData.records || [];
    if (!records.length) {
      body.innerHTML = '<div style="color:#aaa;font-size:12px;padding:8px 0">暂无执行记录</div>';
      return;
    }
    body.innerHTML = `<table class="sched-history" style="width:100%">
      <thead><tr><th>Run ID</th><th>状态</th><th>开始时间</th><th>完成时间</th><th>耗时</th></tr></thead>
      <tbody>${records.map(r => {
        const startT = r.started_at ? new Date(r.started_at).toLocaleString('zh-CN') : '—';
        const endT = r.finished_at ? new Date(r.finished_at).toLocaleString('zh-CN') : '—';
        let duration = '—';
        if (r.started_at && r.finished_at) {
          const ms = new Date(r.finished_at) - new Date(r.started_at);
          duration = ms < 1000 ? ms+'ms' : (ms/1000).toFixed(1)+'s';
        } else if (r.status === 'running') {
          duration = '<span class="spinner" style="width:10px;height:10px;border-width:1.5px;border-color:rgba(124,58,237,.3);border-top-color:#7c3aed"></span>';
        }
        return `<tr>
          <td style="font-family:monospace">${r.run_id}</td>
          <td>${schedBadge(r.status)}</td>
          <td>${startT}</td>
          <td>${endT}</td>
          <td>${duration}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
  } catch(e) {
    body.innerHTML = `<div style="color:#ef4444;font-size:12px">加载失败：${e.message}</div>`;
  }
}

// ====================================================================
// Tab: INSTANCES
// ====================================================================
async function loadInstances() {
  try {
    const res = await fetch(`${API}/api/agents/instances`);
    const data = await res.json();
    const rows = data.instances || [];
    const tbody = document.getElementById('inst-tbody');
    document.getElementById('inst-refresh-hint').textContent = `上次更新 ${fmtTime()}`;
    if (!rows.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">暂无 Agent 实例</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(r => `
      <tr>
        <td style="font-family:monospace;font-size:12px">${r.instance_id||'—'}</td>
        <td>${escHtml(r.agent_id||'—')}</td>
        <td style="font-size:12px;color:#778">${r.image_id||'—'}</td>
        <td>${statusBadge(r.status||'unknown')}</td>
        <td style="text-align:center">${r.ref_count??'—'}</td>
        <td style="font-size:12px;color:#778">${(r.subscribed_workflows||[]).join(', ')||'—'}</td>
      </tr>
    `).join('');
  } catch(e) {
    document.getElementById('inst-tbody').innerHTML =
      `<tr class="empty-row"><td colspan="6">❌ ${e.message}</td></tr>`;
  }
}

// ====================================================================
// Tab: QoS
// ====================================================================
async function loadQos() {
  try {
    const res = await fetch(`${API}/api/qos/metrics`);
    const data = await res.json();
    document.getElementById('qos-refresh-hint').textContent = `上次更新 ${fmtTime()}`;
    // summary cards
    const s = data.summary || {};
    document.getElementById('qos-metrics-row').innerHTML = `
      <div class="metric-card">
        <div class="metric-label">监控 Agent 数</div>
        <div class="metric-value">${s.total_agents??0}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">告警 Agent 数</div>
        <div class="metric-value" style="color:${(s.alert_agents_count||0)>0?'#ef4444':'#22c55e'}">${s.alert_agents_count??0}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">总调用次数</div>
        <div class="metric-value">${s.total_calls??0}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">整体成功率</div>
        <div class="metric-value">${s.overall_success_rate!=null?(s.overall_success_rate*100).toFixed(1)+'%':'—'}</div>
      </div>
    `;
    // table
    const metrics = data.metrics || [];
    const tbody = document.getElementById('qos-tbody');
    if (!metrics.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">暂无 QoS 数据（需先有 Agent 调用记录）</td></tr>';
      return;
    }
    tbody.innerHTML = metrics.map(m => {
      const alerting = m.is_alerting;
      const sr = m.success_rate != null ? (m.success_rate*100).toFixed(1)+'%' : '—';
      return `<tr class="${alerting?'warn-row':''}">
        <td><strong>${escHtml(m.agent_id)}</strong></td>
        <td>${m.total_calls??0}</td>
        <td style="color:#16a34a">${m.success_count??0}</td>
        <td style="color:#dc2626">${m.failure_count??0}</td>
        <td>${m.avg_latency_ms!=null?m.avg_latency_ms.toFixed(0):'-'}</td>
        <td>${m.max_latency_ms!=null?m.max_latency_ms.toFixed(0):'-'}</td>
        <td>${sr}</td>
        <td>${alerting?'<span style="color:#ef4444;font-weight:600">⚠️ 告警</span>':'<span style="color:#16a34a">✅ 正常</span>'}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('qos-tbody').innerHTML =
      `<tr class="empty-row"><td colspan="8">❌ ${e.message}</td></tr>`;
  }
}

// ====================================================================
// Tab: Resources
// ====================================================================
async function loadResources() {
  try {
    const res = await fetch(`${API}/api/resources/`);
    const data = await res.json();
    document.getElementById('res-refresh-hint').textContent = `上次更新 ${fmtTime()}`;
    const s = data.summary || {};
    document.getElementById('res-summary-row').innerHTML = `
      <div class="metric-card">
        <div class="metric-label">总节点数</div>
        <div class="metric-value">${s.total_nodes??0}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">可用节点</div>
        <div class="metric-value" style="color:#22c55e">${s.available_nodes??0}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">可用 CPU 核</div>
        <div class="metric-value">${s.available_cpu!=null?s.available_cpu.toFixed(1):0}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">可用内存 (MB)</div>
        <div class="metric-value">${s.available_memory_mb??0}</div>
      </div>
    `;
    const nodes = data.nodes || [];
    const tbody = document.getElementById('res-tbody');
    if (!nodes.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">暂无节点数据</td></tr>';
      return;
    }
    tbody.innerHTML = nodes.map(n => {
      const cpuUsed = (n.cpu_total - n.cpu_available)||0;
      const memUsed = (n.memory_total_mb - n.memory_available_mb)||0;
      return `<tr>
        <td><strong>${escHtml(n.node_id)}</strong></td>
        <td style="font-family:monospace;font-size:12px">${n.ip||'—'}</td>
        <td>${n.node_type||'—'}</td>
        <td>
          <div>${cpuUsed.toFixed(1)} / ${(n.cpu_total||0).toFixed(1)}</div>
          <div style="height:5px;background:#e8ecf2;border-radius:3px;margin-top:4px">
            <div style="height:100%;width:${Math.min(100,(cpuUsed/(n.cpu_total||1)*100)).toFixed(0)}%;background:#4a6cf7;border-radius:3px"></div>
          </div>
        </td>
        <td>
          <div>${memUsed} / ${n.memory_total_mb||0}</div>
          <div style="height:5px;background:#e8ecf2;border-radius:3px;margin-top:4px">
            <div style="height:100%;width:${Math.min(100,(memUsed/(n.memory_total_mb||1)*100)).toFixed(0)}%;background:#22c55e;border-radius:3px"></div>
          </div>
        </td>
        <td>${n.gpu_count||0}</td>
        <td>${statusBadge(n.status||'unknown')}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('res-tbody').innerHTML =
      `<tr class="empty-row"><td colspan="7">❌ ${e.message}</td></tr>`;
  }
}

// ====================================================================
// Tab: NATS 云边（subject 路由，非 AOE HTTP 转发）
// ====================================================================
let natsConfigLoaded = false;
const NATS_UI_STORAGE_KEY = 'langmanus-nats-cloud-edge-ui';
const _natsActivity = [];

function _natsUiDefaults() {
  return {
    localCluster: 'edge-a',
    targetCluster: 'edge-a',
    peerClusters: 'edge-a,edge-b',
    servers: 'nats://127.0.0.1:4222',
    jetstreamDomain: 'hub',
    streamSubjects: 'workflow.>',
    timeout: 60,
  };
}

function _readNatsUiConfig() {
  const fallback = _natsUiDefaults();
  try {
    return Object.assign(fallback, JSON.parse(localStorage.getItem(NATS_UI_STORAGE_KEY) || '{}'));
  } catch {
    return fallback;
  }
}

function _writeNatsUiConfig() {
  const cfg = {
    localCluster: document.getElementById('nats-local-cluster').value.trim() || 'edge-a',
    targetCluster: document.getElementById('nats-target-cluster').value.trim() || 'edge-a',
    peerClusters: document.getElementById('nats-peer-clusters').value.trim() || 'edge-a,edge-b',
    servers: document.getElementById('nats-servers').value.trim() || 'nats://127.0.0.1:4222',
    jetstreamDomain: document.getElementById('nats-js-domain').value.trim() || 'hub',
    streamSubjects: document.getElementById('nats-stream-subjects').value.trim() || 'workflow.>',
    timeout: parseInt(document.getElementById('nats-timeout').value, 10) || 60,
  };
  localStorage.setItem(NATS_UI_STORAGE_KEY, JSON.stringify(cfg));
  return cfg;
}

function _applyNatsUiConfig(cfg) {
  document.getElementById('nats-local-cluster').value = cfg.localCluster || 'edge-a';
  document.getElementById('nats-target-cluster').value = cfg.targetCluster || 'edge-a';
  document.getElementById('nats-peer-clusters').value = cfg.peerClusters || 'edge-a,edge-b';
  document.getElementById('nats-servers').value = cfg.servers || 'nats://127.0.0.1:4222';
  document.getElementById('nats-js-domain').value = cfg.jetstreamDomain || 'hub';
  document.getElementById('nats-stream-subjects').value = cfg.streamSubjects || 'workflow.>';
  document.getElementById('nats-timeout').value = cfg.timeout || 60;
  document.getElementById('nats-topo-domain').textContent = cfg.jetstreamDomain || 'hub';
  _renderNatsTopology(cfg);
  _refreshNatsSubjectDefaults();
}

function _peerClusterList(cfg) {
  const raw = (cfg.peerClusters || '').split(',').map(s => s.trim()).filter(Boolean);
  const set = new Set(raw);
  set.add(cfg.localCluster);
  set.add(cfg.targetCluster);
  return [...set];
}

function _renderNatsTopology(cfg) {
  const host = document.getElementById('nats-topo-edges');
  if (!host) return;
  const peers = _peerClusterList(cfg);
  host.innerHTML = peers.map(id => {
    const local = id === cfg.localCluster;
    return `<div class="nats-topo-edge ${local ? 'is-local' : ''}">
      <strong>${escHtml(id)}</strong>
      <span>${local ? '当前 kubectl 上下文' : 'leafnode → Hub'}</span>
      <code>nats://nats:4222</code>
    </div>`;
  }).join('');
}

function _safeSubjectToken(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/_/g, '-')
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'agent';
}

function _replySubject(localCluster, workflowId) {
  return `workflow.${localCluster}.reply.${_safeSubjectToken(workflowId)}`;
}

function _natsPublishBody(subject, payload, replySubject) {
  const cfg = _writeNatsUiConfig();
  const body = {
    subject,
    payload,
    reply_subject: replySubject || null,
    stream_subjects: cfg.streamSubjects.split(',').map(s => s.trim()).filter(Boolean),
    timeout_sec: Number(cfg.timeout || 60),
    jetstream_domain: cfg.jetstreamDomain || 'hub',
  };
  const servers = cfg.servers.split(',').map(s => s.trim()).filter(Boolean);
  if (servers.length) body.servers = servers;
  return body;
}

function _refreshNatsSubjectDefaults() {
  const cfg = _readNatsUiConfig();
  const targetSubject = `workflow.${cfg.targetCluster || 'edge-a'}.agent.b.in`;
  const dispatchSubject = document.getElementById('nats-dispatch-subject');
  const chainSubject = document.getElementById('nats-chain-subject');
  const chainCluster = document.getElementById('nats-chain-cluster');
  if (dispatchSubject && !dispatchSubject.dataset.userEdited) dispatchSubject.value = targetSubject;
  if (chainSubject && !chainSubject.dataset.userEdited) chainSubject.value = targetSubject;
  if (chainCluster) chainCluster.value = cfg.targetCluster || 'edge-a';
}

function _appendNatsActivity(kind, subject, detail) {
  _natsActivity.unshift({ t: new Date().toLocaleTimeString('zh-CN'), kind, subject, detail });
  if (_natsActivity.length > 20) _natsActivity.pop();
  const el = document.getElementById('nats-activity-log');
  if (!el) return;
  if (!_natsActivity.length) {
    el.textContent = '暂无记录';
    return;
  }
  el.textContent = _natsActivity.map(row => `[${row.t}] ${row.kind} ${row.subject}\n  ${row.detail}`).join('\n\n');
}

async function loadNatsTab(forceStatus) {
  if (!natsConfigLoaded) {
    await loadNatsConfig(false);
    natsConfigLoaded = true;
  }
  if (forceStatus || activeTab === 'nats') {
    await loadNatsStatus();
    await loadNatsAgents();
  }
}

async function loadNatsConfig(showToast) {
  let cfg = _readNatsUiConfig();
  try {
    const res = await fetch(`${API}/api/comm/nats/config`);
    const data = await res.json();
    if (res.ok && data.config) {
      const c = data.config;
      cfg = Object.assign(cfg, {
        localCluster: c.local_cluster || cfg.localCluster,
        peerClusters: (c.peer_clusters || []).join(','),
        servers: c.servers || cfg.servers,
        jetstreamDomain: c.jetstream_domain || cfg.jetstreamDomain,
        streamSubjects: c.stream_subjects || cfg.streamSubjects,
      });
    }
  } catch {
    /* 使用 localStorage */
  }
  _applyNatsUiConfig(cfg);
  if (showToast !== false) showAlert('nats-alert', 'info', '已加载云边 NATS 配置', 2000);
}

function saveNatsConfig() {
  const cfg = _writeNatsUiConfig();
  const targetSubject = `workflow.${cfg.targetCluster}.agent.b.in`;
  const dispatchSubject = document.getElementById('nats-dispatch-subject');
  const chainSubject = document.getElementById('nats-chain-subject');
  if (dispatchSubject) {
    dispatchSubject.value = targetSubject;
    delete dispatchSubject.dataset.userEdited;
  }
  if (chainSubject) {
    chainSubject.value = targetSubject;
    delete chainSubject.dataset.userEdited;
  }
  document.getElementById('nats-chain-cluster').value = cfg.targetCluster;
  showAlert('nats-alert', 'success', '配置已保存');
}

async function loadNatsStatus() {
  const cfg = _readNatsUiConfig();
  const badge = document.getElementById('nats-conn-badge');
  const detail = document.getElementById('nats-status-detail');
  try {
    const q = encodeURIComponent(cfg.servers);
    const res = await fetch(`${API}/api/comm/nats/status?servers=${q}`);
    const data = await res.json();
    if (data.connected) {
      badge.textContent = '● NATS 已连接';
      badge.className = 'badge-status s-running';
      const stream = data.stream_info;
      const parts = [
        data.servers?.join(', '),
        data.jetstream?.domain ? `domain=${data.jetstream.domain}` : '',
        stream ? `stream=${stream.name} msgs=${stream.messages}` : (data.stream_info_error || ''),
      ].filter(Boolean);
      detail.textContent = `${parts.join(' · ')} · ${fmtTime()}`;
    } else {
      badge.textContent = '● NATS 未连接';
      badge.className = 'badge-status s-error';
      detail.textContent = `${data.error || '连接失败'} · ${data.port_forward_hint || ''} · ${fmtTime()}`;
    }
  } catch (e) {
    badge.textContent = '● 状态未知';
    badge.className = 'badge-status s-unknown';
    detail.textContent = e.message;
  }
}

async function loadNatsAgents() {
  const cfg = _readNatsUiConfig();
  const tbody = document.getElementById('nats-agents-tbody');
  try {
    const res = await fetch(`${API}/api/comm/nats/agents`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
    const agents = data.agents || [];
    document.getElementById('nats-agents-hint').textContent =
      `${data.source} · ${agents.length} agents · ns=${data.namespace || 'default'} · ${fmtTime()}`;
    if (!agents.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="5">未发现 agent-* Deployment</td></tr>';
      return;
    }
    tbody.innerHTML = agents.map(a => `
      <tr class="nats-agent-row" data-subject="${escHtml(a.in_subject)}">
        <td style="font-family:monospace;font-size:12px">${escHtml(a.deployment)}</td>
        <td style="font-family:monospace;font-size:12px">${escHtml(a.cluster_id)}</td>
        <td>${escHtml(a.agent_id)}</td>
        <td style="font-family:monospace;font-size:12px"><a href="#" onclick="useNatsSubject('${escHtml(a.in_subject)}');return false;">${escHtml(a.in_subject)}</a></td>
        <td>${statusBadge(a.status || 'unknown')} ${a.ready_replicas ?? '?'}/${a.replicas ?? '?'}</td>
      </tr>
    `).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5">${escHtml(e.message)}</td></tr>`;
  }
}

function useNatsSubject(subject) {
  const el = document.getElementById('nats-dispatch-subject');
  el.value = subject;
  el.dataset.userEdited = '1';
  showAlert('nats-alert', 'info', `已填入 subject: ${subject}`, 2500);
}

async function dispatchNatsMessage() {
  const text = document.getElementById('nats-task-text').value.trim();
  if (!text) {
    showAlert('nats-alert', 'error', '请填写 Payload 文本');
    return;
  }
  const btn = document.getElementById('nats-dispatch-btn');
  const resultEl = document.getElementById('nats-dispatch-result');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  resultEl.className = 'query-result show';
  resultEl.textContent = '发布中...';
  let subject = '';
  try {
    const cfg = _writeNatsUiConfig();
    const workflowId = document.getElementById('nats-task-id').value.trim() || `ui_${Date.now()}`;
    subject = document.getElementById('nats-dispatch-subject').value.trim()
      || `workflow.${cfg.targetCluster}.agent.b.in`;
    const replySubject = document.getElementById('nats-dispatch-reply').value.trim()
      || _replySubject(cfg.localCluster, workflowId);
    const body = _natsPublishBody(subject, {
      workflow_id: workflowId,
      text,
      reply_subject: replySubject,
      source_cluster_id: cfg.localCluster,
      target_cluster_id: cfg.targetCluster,
    }, replySubject);
    const res = await fetch(`${API}/api/comm/nats/publish`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
    resultEl.textContent = JSON.stringify(data, null, 2);
    _appendNatsActivity('PUB', subject, `seq=${data.seq} reply=${data.reply_status || 'n/a'}`);
    showAlert('nats-alert', 'success', `已发布到 ${subject}`);
    await loadNatsStatus();
  } catch (e) {
    resultEl.textContent = `错误：${e.message}`;
    _appendNatsActivity('ERR', subject || '-', e.message);
    showAlert('nats-alert', 'error', e.message, 0);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'JetStream 发布';
  }
}

async function testNatsAgentChain() {
  const text = document.getElementById('nats-chain-text').value.trim();
  if (!text) {
    showAlert('nats-alert', 'error', '请填写测试文本');
    return;
  }
  const btn = document.getElementById('nats-chain-btn');
  const resultEl = document.getElementById('nats-chain-result');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  resultEl.className = 'query-result show';
  resultEl.textContent = '验证中...';
  let subject = '';
  try {
    const cfg = _writeNatsUiConfig();
    const targetCluster = document.getElementById('nats-chain-cluster').value.trim() || cfg.targetCluster;
    const workflowId = document.getElementById('nats-chain-workflow-id').value.trim() || `ui_${Date.now()}`;
    subject = document.getElementById('nats-chain-subject').value.trim()
      || `workflow.${targetCluster}.agent.b.in`;
    const replySubject = document.getElementById('nats-chain-reply').value.trim()
      || _replySubject(cfg.localCluster, workflowId);
    const body = _natsPublishBody(subject, {
      workflow_id: workflowId,
      text,
      reply_subject: replySubject,
      source_cluster_id: cfg.localCluster,
      target_cluster_id: targetCluster,
    }, replySubject);
    const res = await fetch(`${API}/api/comm/nats/publish`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
    const reply = data.reply || null;
    resultEl.textContent = JSON.stringify(data, null, 2);
    const ok = data.reply_status === 'received';
    _appendNatsActivity(ok ? 'CHAIN OK' : 'CHAIN', subject, reply?.result || data.reply_status);
    showAlert(
      'nats-alert',
      ok ? 'success' : 'error',
      ok ? `链路成功：${reply?.result || '已收到回复'}` : `已发布但未收到回复：${replySubject}`
    );
  } catch (e) {
    resultEl.textContent = `错误：${e.message}`;
    _appendNatsActivity('ERR', subject || '-', e.message);
    showAlert('nats-alert', 'error', e.message, 0);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '验证 B → C';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  ['nats-dispatch-subject', 'nats-chain-subject'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => { el.dataset.userEdited = '1'; });
  });
});

// ====================================================================
// Auto-refresh & init
// ====================================================================
async function checkServer() {
  try {
    const res = await fetch(`${API}/api/apps/`);
    document.getElementById('server-status').textContent = res.ok ? '● 已连接' : '● 异常';
    document.getElementById('server-status').style.color = res.ok ? '#4ade80' : '#f87171';
  } catch {
    document.getElementById('server-status').textContent = '● 离线';
    document.getElementById('server-status').style.color = '#f87171';
  }
}

// Load initial tab and start auto-refresh
loadWarehouseImages();
loadApps();
checkServer();
setInterval(() => TAB_LOADERS[activeTab](), 5000);
setInterval(checkServer, 10000);
