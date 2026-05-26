// App details page with merged visualization tabs.
const API = '';

let currentApp = null;
let wsWorkflow = null;
let cy = null;

const vizState = {
  workflowId: '',
  summary: null,
  snapshot: null,
  pane1SigByWf: {},
  pane1PlatformsSigByWf: {},
};

const VIEW_KEY_PREFIX = 'app_details_view_';

const STATUS_COLORS = {
  pending: { bg: '#f1f5f9', border: '#94a3b8' },
  running: { bg: '#dbeafe', border: '#2563eb' },
  completed: { bg: '#dcfce7', border: '#16a34a' },
  failed: { bg: '#fee2e2', border: '#dc2626' },
};

function getQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  return params.get(name);
}

function setActiveTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
  if (name === 'topology' && cy) {
    setTimeout(() => {
      try {
        cy.resize();
        cy.fit(undefined, 40);
      } catch (e) {
        console.warn('resize cy failed', e);
      }
    }, 0);
  }
}

function toggleCollapse(header) {
  header.classList.toggle('open');
  header.nextElementSibling.classList.toggle('open');
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function ensureArray(value) {
  return Array.isArray(value) ? value : [];
}

function byId(id) {
  return document.getElementById(id);
}

function saveViewState() {
  if (!vizState.workflowId || !window.localStorage || !cy) return;
  try {
    const view = { pan: cy.pan(), zoom: cy.zoom() };
    localStorage.setItem(VIEW_KEY_PREFIX + vizState.workflowId, JSON.stringify(view));
  } catch (error) {
    console.warn('saveViewState failed', error);
  }
}

function loadViewState(workflowId) {
  if (!workflowId || !window.localStorage) return null;
  try {
    const raw = localStorage.getItem(VIEW_KEY_PREFIX + workflowId);
    return raw ? JSON.parse(raw) : null;
  } catch (error) {
    return null;
  }
}

function normalizeAgentViewUrl(url) {
  if (!url) {
    return '';
  }

  const normalized = String(url).trim();
  try {
    const parsed = new URL(normalized);
    if (parsed.hostname === '192.168.49.2' && parsed.port === '30092') {
      parsed.hostname = '127.0.0.1';
      return parsed.toString();
    }
  } catch (error) {
    return normalized;
  }

  return normalized;
}

function setLogicForm(app) {
  const guidance = app && app.guidance_file ? app.guidance_file : {};
  byId('e-name').value = app?.name || '';
  byId('e-task').value = guidance.task_description || '';
  byId('e-mode').value = guidance.orchestration_mode || 'adaptive';
  byId('e-skills').value = guidance.skills_content || '';
}

function setRuntimeInfo(app) {
  byId('toolbar-status').textContent = app?.status || '—';
  byId('toolbar-workflow-handle').textContent = app?.workflow_handle || '—';

  const agentsHost = byId('agents-host');
  if (agentsHost) {
    agentsHost.innerHTML = '<div class="empty-state">正在加载智能体视图...</div>';
  }
}

function renderAgentViews(views) {
  const agentsHost = byId('agents-host');
  if (!agentsHost) {
    return;
  }

  agentsHost.innerHTML = '';
  if (!Array.isArray(views) || !views.length) {
    agentsHost.innerHTML = '<div class="empty-state">当前应用暂无可嵌入的智能体前端视图。</div>';
    return;
  }

  views.forEach((view, index) => {
    const el = document.createElement('div');
    el.className = 'agent-view-card';

    const frontendUrl = normalizeAgentViewUrl(view.frontend_url);
    const title = view.capability || view.agent_id || `智能体 ${index + 1}`;
    el.innerHTML = `
      <div class="agent-view-header">
        <div>
          <div class="agent-view-title">${escapeHtml(title)}</div>
          <div class="agent-view-meta">
            <span>${escapeHtml(view.agent_id || '—')}</span>
            <span>${escapeHtml(view.image_id || '—')}</span>
            <span>${escapeHtml(view.ip || '—')}:${escapeHtml(view.port || '—')}</span>
            <span>${escapeHtml(view.status || '—')}</span>
          </div>
        </div>
        <a class="agent-view-open" href="${escapeHtml(frontendUrl || '#')}" target="_blank" rel="noreferrer">打开页面</a>
      </div>
      <div class="agent-view-frame-wrap">
        ${frontendUrl ? `<iframe class="agent-view-frame" src="${escapeHtml(frontendUrl)}" loading="lazy" referrerpolicy="no-referrer"></iframe>` : '<div class="empty-state">未找到可访问的前端地址</div>'}
      </div>
    `;
    agentsHost.appendChild(el);
  });
}

async function loadAgentViews(appId) {
  const isSenseDemo = appId === 'app_sense_demo';
  const senseDemoView = {
    capability: 'cooperativefeaturefusiondetectionviz',
    agent_id: 'cooperativefeaturefusiondetectionviz_agent_001',
    image_id: 'cooperativefeaturefusiondetectionviz',
    ip: '10.112.221.121',
    port: 9002,
    status: 'running',
    frontend_url: 'http://10.112.221.121:9002',
  };

  const agentsHost = byId('agents-host');
  if (agentsHost) {
    agentsHost.innerHTML = '<div class="empty-state">正在加载智能体视图...</div>';
  }

  try {
    const viewsResponse = await fetch(`${API}/api/apps/${encodeURIComponent(appId)}/agent-views`);
    if (!viewsResponse.ok) {
      throw new Error((await viewsResponse.json()).detail || '加载智能体视图失败');
    }
    const viewsData = await viewsResponse.json();
    const views = Array.isArray(viewsData.views) ? [...viewsData.views] : [];
    if (isSenseDemo) {
      const exists = views.some(view => (view.capability || '').toLowerCase() === 'cooperativefeaturefusiondetectionviz');
      if (!exists) {
        views.push(senseDemoView);
      }
    }
    renderAgentViews(views);
  } catch (error) {
    if (isSenseDemo) {
      renderAgentViews([senseDemoView]);
      return;
    }
    renderAgentViews([]);
    if (agentsHost) {
      agentsHost.innerHTML = `<div class="empty-state">加载智能体视图失败：${escapeHtml(error.message)}</div>`;
    }
  }
}

function hasMeaningfulOrchestration(o) {
  if (!o || typeof o !== 'object') return false;
  const hasSkills = Boolean((o.skills_content || '').trim());
  const hasPipeline = ensureArray(o.pipeline_topology).length > 0;
  const hasAgents = ensureArray(o.available_agents).length > 0;
  const hasSelected = ensureArray(o.selected_agents).length > 0;
  return hasSkills || hasPipeline || hasAgents || hasSelected;
}

function getStablePlatformSig(o) {
  if (!o || !Array.isArray(o.available_agents)) return '';
  return o.available_agents
    .slice()
    .sort((a, b) => String(a.id || '').localeCompare(String(b.id || '')))
    .map(a => [
      a.id || '',
      a.platform || '',
      a.platform_key || '',
      a.capability || '',
      a.ip || '',
      String(a.port || ''),
      a.is_local ? '1' : '0',
    ].join('|')).join(';;');
}

function shouldIgnoreScheduleEmptyOrchestration(o, summary, hasSig) {
  if (!summary || summary.view_type !== 'schedule') return false;
  if (!hasSig) return false;
  const noSkills = !((o && o.skills_content) || '').trim();
  const noPipeline = ensureArray(o && o.pipeline_topology).length === 0;
  const noSelected = ensureArray(o && o.selected_agents).length === 0;
  return noSkills && noPipeline && noSelected;
}

function renderPipelineFlow(o) {
  const pf = byId('pipelineFlow');
  if (!pf) return;
  pf.innerHTML = '';

  const items = ensureArray(o.pipeline_topology);
  if (!items.length) {
    pf.innerHTML = '<span class="empty-state">无固定 Pipeline，使用 Planner 动态规划</span>';
    return;
  }

  const createStepEl = (step) => {
    const el = document.createElement('div');
    el.className = 'viz-pipe-step';
    el.textContent = step.step || step.capability || step.description || step.agent_id || '';
    return el;
  };

  let i = 0;
  while (i < items.length) {
    const it = items[i] || {};
    const isArrayGroup = Array.isArray(it);
    const group = !isArrayGroup && (it.parallel_group || '');

    if (isArrayGroup || group) {
      const wrap = document.createElement('div');
      wrap.className = 'viz-pipe-parallel';
      if (isArrayGroup) {
        it.forEach((step) => {
          if (step && typeof step === 'object') {
            wrap.appendChild(createStepEl(step));
          }
        });
        i += 1;
      } else {
        let j = i;
        while (j < items.length && items[j] && items[j].parallel_group === group) {
          const s = items[j];
          if (s && typeof s === 'object' && !Array.isArray(s)) {
            wrap.appendChild(createStepEl(s));
          }
          j += 1;
        }
        i = j;
      }
      pf.appendChild(wrap);
    } else {
      if (it && typeof it === 'object') {
        pf.appendChild(createStepEl(it));
      }
      i += 1;
    }

    if (i < items.length) {
      const arrow = document.createElement('span');
      arrow.className = 'viz-pipe-arrow';
      arrow.textContent = '→';
      pf.appendChild(arrow);
    }
  }
}

function renderPane1(o) {
  const data = o || {};
  byId('skillsContent').textContent = data.skills_content || '(空)';
  byId('m-complexity').textContent = data.complexity_level || '-';
  byId('m-mode').textContent = data.orchestration_mode || '-';
  byId('m-total').textContent = ensureArray(data.available_agents).length;
  byId('m-local').textContent = ensureArray(data.local_agents).length;
  byId('m-remote').textContent = ensureArray(data.remote_agents).length;
  byId('m-selected').textContent = ensureArray(data.selected_agents).length;

  const wfId = vizState.workflowId || '__none__';
  const nextPlatformSig = getStablePlatformSig(data);
  const previousPlatformSig = vizState.pane1PlatformsSigByWf[wfId] || '';
  if (nextPlatformSig !== previousPlatformSig) {
    vizState.pane1PlatformsSigByWf[wfId] = nextPlatformSig;
    const box = byId('platformsBox');
    box.innerHTML = '';

    const groups = {};
    ensureArray(data.available_agents).forEach(a => {
      const key = a.platform_key || `${a.ip || '-'}:${a.port || '-'}`;
      if (!groups[key]) {
        groups[key] = { platform: a.platform || 'remote', agents: [] };
      }
      groups[key].agents.push(a);
    });

    Object.entries(groups).forEach(([key, item]) => {
      const block = document.createElement('div');
      block.className = 'viz-platform-block';
      const platformLabel = item.platform === 'local' ? '🏠 本机' : '☁️ 远端';
      block.innerHTML = `
        <div class="viz-platform-title">
          <div class="name">${escapeHtml(platformLabel)} · ${escapeHtml(key)}</div>
          <div class="count">${item.agents.length} agents</div>
        </div>
        <div class="viz-agent-grid">
          ${item.agents.map(agent => `
            <div class="viz-agent-chip ${agent.is_selected ? 'selected' : ''} ${agent.status === 'busy' ? 'busy' : ''}" title="${escapeHtml(agent.description || '')}">
              <div>${escapeHtml(agent.id || '-')}</div>
              <div class="cap">${escapeHtml(agent.capability || '-')}
                · ${escapeHtml(agent.status || '-')}</div>
            </div>
          `).join('')}
        </div>
      `;
      box.appendChild(block);
    });

    if (!Object.keys(groups).length) {
      box.innerHTML = '<div class="empty-state">暂无候选 Agent</div>';
    }
  }

  renderPipelineFlow(data);
}

function initCy() {
  if (typeof cytoscape === 'undefined') return;
  cy = cytoscape({
    container: byId('cy'),
    elements: [],
    style: [
      {
        selector: 'node',
        style: {
          'background-color': 'data(bgcolor)',
          'border-color': 'data(bordercolor)',
          'border-width': 2,
          label: 'data(label)',
          color: '#1f2937',
          'font-size': 11,
          'text-valign': 'center',
          'text-halign': 'center',
          'text-wrap': 'wrap',
          'text-max-width': '120px',
          shape: 'round-rectangle',
          width: 140,
          height: 50,
          padding: '6px',
        },
      },
      {
        selector: 'node.current',
        style: {
          'border-width': 4,
          'border-color': '#2563eb',
        },
      },
      {
        selector: 'node.platform',
        style: {
          shape: 'round-tag',
          'background-color': '#f8fafc',
          'border-color': 'data(bordercolor)',
          'border-style': 'dashed',
          color: '#334155',
          'font-size': 12,
          'font-weight': 700,
          padding: '10px',
          'text-valign': 'top',
          'text-margin-y': -8,
        },
      },
      {
        selector: 'edge',
        style: {
          width: 2,
          'line-color': '#94a3b8',
          'target-arrow-color': '#94a3b8',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
        },
      },
      {
        selector: 'edge.parallel_start, edge.parallel_group',
        style: {
          'line-color': '#7c3aed',
          'target-arrow-color': '#7c3aed',
          'line-style': 'dashed',
        },
      },
      {
        selector: 'edge.active',
        style: {
          'line-color': '#2563eb',
          'target-arrow-color': '#2563eb',
          width: 3,
        },
      },
    ],
    layout: { name: 'dagre', rankDir: 'LR', nodeSep: 50, rankSep: 90 },
    wheelSensitivity: 0.2,
  });
  cy.on('pan zoom', () => {
    saveViewState();
  });
}

function renderPane2(topology) {
  const t = topology || {};
  const counts = t.counts || {};

  byId('s-total').textContent = t.total || 0;
  byId('s-completed').textContent = counts.completed || 0;
  byId('s-running').textContent = counts.running || 0;
  byId('s-failed').textContent = counts.failed || 0;
  byId('s-cross').textContent = t.cross_host_count || 0;
  byId('s-sched-total').textContent = t.schedule_total_runs || 0;
  byId('s-sched-failed').textContent = t.schedule_failed_runs || 0;

  if (!cy) {
    initCy();
  }
  if (!cy) return;

  const elements = [];
  ensureArray(t.platforms).forEach(p => {
    elements.push({
      data: {
        id: `platform_${p.key}`,
        label: `${p.platform === 'local' ? '🏠 本机' : '☁️ 远端'} ${p.key}`,
        bordercolor: p.platform === 'local' ? '#3b82f6' : '#8b5cf6',
      },
      classes: 'platform',
    });
  });

  ensureArray(t.nodes).forEach(n => {
    const color = STATUS_COLORS[n.status] || STATUS_COLORS.pending;
    const key = `${n.ip || '-'}:${n.port || '-'}`;
    const hasPlatform = ensureArray(t.platforms).some(p => p.key === key);
    elements.push({
      data: {
        id: n.id,
        parent: hasPlatform ? `platform_${key}` : undefined,
        label: `${n.title || n.id}\n[${n.agent_id || '-'}]`,
        bgcolor: color.bg,
        bordercolor: color.border,
      },
      classes: n.is_current ? 'current' : '',
    });
  });

  ensureArray(t.edges).forEach(e => {
    elements.push({
      data: { id: `${e.from}->${e.to}`, source: e.from, target: e.to },
      classes: `${e.type || ''}${ensureArray(t.nodes).some(n => n.id === e.to && n.is_current) ? ' active' : ''}`.trim(),
    });
  });

  cy.elements().remove();
  cy.add(elements);
  const savedView = loadViewState(vizState.workflowId);
  try {
    cy.layout({ name: 'dagre', rankDir: 'LR', nodeSep: 40, rankSep: 95, fit: false, animate: false }).run();
  } catch (e) {
    console.warn('layout failed', e);
  }

  if (savedView && savedView.pan && savedView.zoom !== undefined) {
    try {
      cy.zoom(savedView.zoom);
      cy.pan(savedView.pan);
    } catch (error) {
      console.warn('restore view failed', error);
    }
  }
}

function renderPane3(execution) {
  const e = execution || {};
  const counts = e.counts || {};
  const mag = e.magentic || {};

  const progress = Number(e.progress_percent || 0);
  byId('p-pct').textContent = `${progress}%`;
  byId('progressFill').style.width = `${progress}%`;
  byId('p-frac').textContent = `${counts.completed || 0} / ${e.total || 0}`;
  byId('p-failed').textContent = counts.failed || 0;
  byId('p-replan').textContent = e.replanning_count || 0;
  byId('m1-round').textContent = `${mag.round || 0} / ${mag.max_round || '-'}`;
  byId('m1-stall').textContent = mag.stall_count || 0;
  byId('m1-mode').textContent = mag.mode || '-';

  const cb = byId('currentBox');
  const cur = e.current || {};
  if (cur && cur.status && cur.status !== 'pending') {
    cb.className = 'viz-current-card';
    cb.innerHTML = `
      <div class="title">${escapeHtml(cur.title || '当前任务')}</div>
      <div class="meta">
        <span class="meta-tag">🤖 ${escapeHtml(cur.agent_id || '-')}</span>
        <span class="meta-tag">📡 ${escapeHtml((cur.protocol || '?').toUpperCase())}</span>
        <span class="meta-tag">🛠 ${escapeHtml(cur.executor || '-')}</span>
        <span class="meta-tag">📍 ${escapeHtml(cur.ip || '-')}:${escapeHtml(cur.port || '-')}</span>
      </div>
      ${ensureArray(cur.tools_called).length ? `<div class="viz-tools">${ensureArray(cur.tools_called).map(item => {
        const name = typeof item === 'string' ? item : item.tool;
        return `<span class="viz-tool-tag">${escapeHtml(name || '-')}</span>`;
      }).join('')}</div>` : ''}
    `;
  } else {
    cb.className = 'empty-state';
    cb.textContent = e.all_completed ? '全部任务已完成' : '暂无执行任务';
  }

  const timeline = byId('timelineList');
  timeline.innerHTML = '';
  ensureArray(e.timeline).forEach(item => {
    const li = document.createElement('li');
    li.className = item.status || '';
    const tools = ensureArray(item.tools_called).map(c => {
      const name = typeof c === 'string' ? c : c.tool;
      return `<span class="viz-tool-tag">${escapeHtml(name || '-')}</span>`;
    }).join('');
    li.innerHTML = `
      <div class="ti-title">${escapeHtml(item.title || '-')} <span style="font-size:11px;color:#64748b">[${escapeHtml(item.status || '-')}]</span></div>
      <div class="ti-meta">🤖 ${escapeHtml(item.agent_id || '-')} · 📡 ${escapeHtml((item.protocol || '?').toUpperCase())} · ⏱ ${item.duration_ms ? `${item.duration_ms}ms` : '-'}</div>
      ${tools ? `<div style="margin-top:4px">${tools}</div>` : ''}
    `;
    timeline.appendChild(li);
  });
}

function renderVizAll() {
  if (!vizState.snapshot) {
    return;
  }
  const wfId = vizState.workflowId || '__none__';
  const orchestration = vizState.snapshot.orchestration || {};
  const previousSig = vizState.pane1SigByWf[wfId] || '';

  if (shouldIgnoreScheduleEmptyOrchestration(orchestration, vizState.summary, Boolean(previousSig))) {
    // Ignore transient empty schedule snapshots.
  } else if (hasMeaningfulOrchestration(orchestration)) {
    const nextSig = JSON.stringify(orchestration);
    if (nextSig !== previousSig) {
      vizState.pane1SigByWf[wfId] = nextSig;
      renderPane1(orchestration);
    }
  } else if (!previousSig) {
    renderPane1(orchestration);
  }

  renderPane2(vizState.snapshot.topology || {});
  renderPane3(vizState.snapshot.execution || {});
}

function clearVizPanels(message) {
  byId('skillsContent').textContent = message;
  byId('platformsBox').innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
  byId('pipelineFlow').innerHTML = `<span class="empty-state">${escapeHtml(message)}</span>`;
  byId('currentBox').className = 'empty-state';
  byId('currentBox').textContent = message;
  byId('timelineList').innerHTML = `<li class="empty-state" style="border:none;padding:0;margin:0">${escapeHtml(message)}</li>`;
}

async function resolveVizWorkflowId(app) {
  if (app?.workflow_handle) {
    return app.workflow_handle;
  }

  try {
    const res = await fetch('/api/viz/workflows?limit=100');
    if (!res.ok) return '';
    const data = await res.json();
    const list = ensureArray(data.workflows);
    const exact = list.find(w => w.app_id === app?.app_id);
    if (exact) return exact.id;
    const guess = list.find(w => String(w.title || '').includes(String(app?.app_id || '')));
    return guess ? guess.id : '';
  } catch (error) {
    console.warn('resolve viz workflow failed', error);
    return '';
  }
}

function closeVizSocket() {
  if (wsWorkflow) {
    try {
      wsWorkflow.close();
    } catch (e) {
      console.warn('close ws failed', e);
    }
    wsWorkflow = null;
  }
}

function connectVizSocket(wfId) {
  closeVizSocket();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  wsWorkflow = new WebSocket(`${proto}://${location.host}/ws/viz/workflows/${encodeURIComponent(wfId)}`);
  wsWorkflow.onmessage = event => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'snapshot') {
      vizState.snapshot = msg.data || null;
      vizState.summary = msg.summary || vizState.summary;
      renderVizAll();
    }
  };
  wsWorkflow.onerror = err => {
    console.warn('viz ws error', err);
  };
}

async function bindVizForCurrentApp() {
  const wfId = await resolveVizWorkflowId(currentApp);
  if (!wfId) {
    vizState.workflowId = '';
    vizState.summary = null;
    vizState.snapshot = null;
    closeVizSocket();
    clearVizPanels('当前应用暂无可视化工作流，请先启动应用。');
    return;
  }

  vizState.workflowId = wfId;
  vizState.pane1SigByWf[wfId] = vizState.pane1SigByWf[wfId] || '';

  try {
    const response = await fetch(`/api/viz/workflows/${encodeURIComponent(wfId)}/full`);
    if (!response.ok) {
      throw new Error(`工作流快照加载失败(${response.status})`);
    }
    vizState.snapshot = await response.json();
    renderVizAll();
  } catch (error) {
    console.warn('load viz snapshot failed', error);
    clearVizPanels(`可视化快照加载失败：${error.message}`);
  }

  connectVizSocket(wfId);
}

async function loadAppDetail(appId) {
  const appsResponse = await fetch(`${API}/api/apps/`);
  if (!appsResponse.ok) {
    throw new Error(`读取应用列表失败(${appsResponse.status})`);
  }

  const appsData = await appsResponse.json();
  currentApp = ensureArray(appsData.apps).find(app => app.app_id === appId) || null;

  if (!currentApp) {
    throw new Error(`应用 ${appId} 不存在`);
  }

  setLogicForm(currentApp);
  setRuntimeInfo(currentApp);
  await loadAgentViews(appId);
  await bindVizForCurrentApp();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab-btn').forEach(button => {
    button.addEventListener('click', () => setActiveTab(button.dataset.tab));
  });

  const parts = window.location.pathname.split('/');
  const appId = parts[parts.length - 1] || getQueryParam('app_id');
  byId('app-id').textContent = appId;

  loadAppDetail(appId).catch(error => {
    byId('e-name').value = '';
    byId('e-task').value = '';
    byId('e-mode').value = 'adaptive';
    byId('e-skills').value = '';
    byId('toolbar-status').textContent = '—';
    byId('toolbar-workflow-handle').textContent = '—';
    byId('agents-host').textContent = '加载失败';
    clearVizPanels(`加载失败：${error.message}`);
  });

  byId('save-logic').addEventListener('click', async () => {
    const name = byId('e-name').value.trim();
    const taskDescription = byId('e-task').value.trim();
    const orchestrationMode = byId('e-mode').value;
    const skillsMd = byId('e-skills').value.trim();

    if (!name || !taskDescription) {
      alert('请填写应用名称和任务描述');
      return;
    }

    try {
      const res = await fetch(`${API}/api/apps/${encodeURIComponent(appId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          task_description: taskDescription,
          orchestration_mode: orchestrationMode,
          skills_md: skillsMd,
        }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || '保存失败');

      currentApp = {
        ...currentApp,
        name,
        guidance_file: {
          ...(currentApp && currentApp.guidance_file ? currentApp.guidance_file : {}),
          task_description: taskDescription,
          orchestration_mode: orchestrationMode,
          skills_content: skillsMd,
        },
      };
      setRuntimeInfo(currentApp);
      await loadAgentViews(appId);
      alert('保存成功');
    } catch (error) {
      alert(`保存失败：${error.message}`);
    }
  });

  byId('btn-start')?.addEventListener('click', async () => {
    try {
      const res = await fetch(`${API}/api/apps/${encodeURIComponent(appId)}/start`, { method: 'POST' });
      if (!res.ok) throw new Error((await res.json()).detail || '启动失败');
      await loadAppDetail(appId);
      alert('启动成功');
    } catch (error) {
      alert(`启动失败：${error.message}`);
    }
  });

  byId('btn-stop')?.addEventListener('click', async () => {
    try {
      const res = await fetch(`${API}/api/apps/${encodeURIComponent(appId)}/stop`, { method: 'POST' });
      if (!res.ok) throw new Error((await res.json()).detail || '停止失败');
      await loadAppDetail(appId);
      alert('停止成功');
    } catch (error) {
      alert(`停止失败：${error.message}`);
    }
  });

  byId('btn-reload')?.addEventListener('click', async () => {
    await loadAppDetail(appId);
  });
});
