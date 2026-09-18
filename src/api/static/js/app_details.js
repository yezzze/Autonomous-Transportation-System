// App details page with merged visualization tabs.
const API = '';

let currentApp = null;
let wsWorkflow = null;
let cy = null;
let runtimeRefreshTimer = null;
let runtimeRefreshBusy = false;
let workflowTrendTimer = null;
let workflowTrendRequestId = 0;

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
const AGENT_TYPE_COLORS = {
  business: '#dbeafe',
  resource: '#fef3c7',
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
  if (name === 'execution') {
    loadWorkflowTrends();
    if (workflowTrendTimer === null) {
      workflowTrendTimer = window.setInterval(loadWorkflowTrends, 15000);
    }
  } else if (workflowTrendTimer !== null) {
    window.clearInterval(workflowTrendTimer);
    workflowTrendTimer = null;
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

function appStatusLabel(status) {
  const labels = {
    undeployed: '未部署', deploying: '部署中', deployed: '已部署',
    undeploying: '取消部署中', deployment_error: '部署错误',
    not_running: '未运行', starting: '启动中', running: '运行中',
    stopping: '停止中', stopped: '已停止', completed: '运行完成',
    run_error: '运行错误',
  };
  return labels[status] || status || '—';
}

function setRuntimeInfo(app) {
  byId('toolbar-deployment-status').textContent = appStatusLabel(app?.deployment_status);
  byId('toolbar-run-status').textContent = appStatusLabel(app?.run_status);
  byId('toolbar-workflow-handle').textContent = preferredWorkflowHandle(app) || '—';
  if (byId('exec-summary-status')) {
    renderExecutionSummary(vizState.snapshot?.execution || {});
  }

  const agentsHost = byId('agents-host');
  if (agentsHost) {
    agentsHost.innerHTML = '<div class="empty-state">正在加载智能体视图...</div>';
  }
}

function preferredWorkflowHandle(app) {
  // 应用详情始终订阅唯一的对外主句柄。周期会话与各次执行使用内部 ID，
  // 其状态由后端汇总发布到该主句柄。
  return app?.workflow_handle || '';
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
    ip: '10.112.93.73',
    port: 9032,
    status: 'running',
    frontend_url: 'http://10.112.93.73:9032',
  };

  const isSimlingoDemo = appId === 'simlingo_demo';
  const simlingoDemoView = {
    capability: 'simlingoLLM',
    agent_id: 'simlingo-llm-agent',
    image_id: 'simlingo-llm-agent:0.3.1',
    ip: '10.112.93.73',
    port: 8899,
    status: 'running',
    frontend_url: 'http://10.112.93.73:8899',
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
    if (isSimlingoDemo) {
      const exists = views.some(view => (view.capability || '').toLowerCase() === 'simlingollm');
      if (!exists) {
        views.push(simlingoDemoView);
      }
    }
    renderAgentViews(views);
  } catch (error) {
    if (isSenseDemo) {
      renderAgentViews([senseDemoView]);
      return;
    }
    if (isSimlingoDemo) {
      renderAgentViews([simlingoDemoView]);
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
        bgcolor: AGENT_TYPE_COLORS[n.agent_type] || AGENT_TYPE_COLORS.business,
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
  renderExecutionSummary(e);
}

function formatExecutionTimestamp(value) {
  if (value == null || value === '') return '—';
  const numeric = Number(value);
  const date = Number.isFinite(numeric) ? new Date(numeric * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN');
}

function renderExecutionSummary(execution) {
  const e = execution || {};
  const counts = e.counts || {};
  const summary = vizState.summary || {};
  const guidance = currentApp?.guidance_file || {};
  const scheduled = summary.view_type === 'schedule' || Boolean(currentApp?.schedule_active);
  const deployOnly = Boolean(guidance.metadata?.deploy_only);
  const elapsed = Number(summary.elapsed);

  const displayedRunStatus = vizState.snapshot?.deployment_only === true
    ? currentApp?.run_status
    : (summary.status || currentApp?.run_status);
  byId('exec-summary-status').textContent = appStatusLabel(displayedRunStatus);
  byId('exec-summary-elapsed').textContent = Number.isFinite(elapsed) ? `${elapsed.toFixed(2)} 秒` : '—';
  byId('exec-summary-started').textContent = formatExecutionTimestamp(summary.started_at);
  byId('exec-summary-updated').textContent = formatExecutionTimestamp(summary.updated_at);
  byId('exec-summary-progress').textContent = `${counts.completed || 0} / ${e.total || 0}`;
  byId('exec-summary-failed').textContent = counts.failed || 0;
  byId('exec-summary-execution-mode').textContent = scheduled ? '周期执行' : '单次执行';
  byId('exec-summary-deployment-mode').textContent = deployOnly ? 'deploy_only' : '普通模式';
  const completedSummary = byId('completed-workflow-summary');
  if (completedSummary) completedSummary.hidden = currentApp?.run_status !== 'completed';
}

function formatDurationSeconds(value) {
  const number = Number(value);
  return Number.isFinite(number)
    ? `${number.toLocaleString('zh-CN', {maximumFractionDigits: 3})} 秒`
    : '暂无数据';
}

function renderCompletedWorkflowSummary(data) {
  const host = byId('completed-workflow-summary');
  if (!host) return;
  const completed = currentApp?.run_status === 'completed';
  host.hidden = !completed;
  if (!completed) return;
  const summary = data?.workflow_summary || {};
  byId('completed-total-duration').textContent = formatDurationSeconds(summary.total_duration_seconds);
  const count = Number(summary.execution_count);
  byId('completed-execution-count').textContent = Number.isFinite(count)
    ? count.toLocaleString('zh-CN', {maximumFractionDigits: 0})
    : '暂无数据';
  byId('completed-average-duration').textContent = formatDurationSeconds(summary.average_duration_seconds);
}

const WORKFLOW_TREND_CHARTS = {
  average_duration: {prefix: 'trend-average', axisRange: null},
  p95_duration: {prefix: 'trend-p95', axisRange: null},
  execution_count: {prefix: 'trend-count', axisRange: null},
  failure_rate: {prefix: 'trend-failure', axisRange: null},
};

function workflowTrendElements(config) {
  return {
    panel: byId(`${config.prefix}-chart`).closest('.prometheus-chart-panel'),
    chart: byId(`${config.prefix}-chart`),
    legend: byId(`${config.prefix}-legend`),
    tooltip: byId(`${config.prefix}-tooltip`),
    get axisRange() { return config.axisRange; },
    set axisRange(value) { config.axisRange = value; },
  };
}

async function loadWorkflowTrends() {
  if (!currentApp?.app_id || !byId('panel-execution')?.classList.contains('active')) return;
  const requestId = ++workflowTrendRequestId;
  const refresh = byId('refresh-workflow-trends');
  refresh.disabled = true;
  Object.values(WORKFLOW_TREND_CHARTS).forEach(config => {
    byId(`${config.prefix}-state`).textContent = '加载中...';
  });
  if (currentApp.run_status === 'completed') {
    ['completed-total-duration', 'completed-execution-count', 'completed-average-duration']
      .forEach(id => { byId(id).textContent = '加载中...'; });
  }
  try {
    const rangeSeconds = Number(byId('workflow-trend-range').value);
    const response = await fetch(`${API}/api/apps/${encodeURIComponent(currentApp.app_id)}/prometheus-metrics?range_seconds=${rangeSeconds}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    if (requestId !== workflowTrendRequestId) return;
    const unavailable = data.unavailable_metrics || [];
    renderCompletedWorkflowSummary(data);
    Object.entries(WORKFLOW_TREND_CHARTS).forEach(([key, config]) => {
      renderPrometheusChart(
        data.metrics?.[key] || {result: []},
        workflowTrendElements(config),
        {
          xMin: data.query_range?.start,
          xMax: data.query_range?.end,
          sampleStep: data.query_range?.step,
        },
      );
      byId(`${config.prefix}-state`).textContent = unavailable.includes(key) ? '暂不可用' : '每 15 秒更新';
    });
  } catch (error) {
    if (requestId !== workflowTrendRequestId) return;
    Object.values(WORKFLOW_TREND_CHARTS).forEach(config => {
      byId(`${config.prefix}-chart`).innerHTML = `<div class="empty-state" style="padding-top:70px">加载失败：${escapeHtml(error.message)}</div>`;
      byId(`${config.prefix}-state`).textContent = '加载失败';
    });
  } finally {
    if (requestId === workflowTrendRequestId) refresh.disabled = false;
  }
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
  renderExecutionSummary({});
}

async function resolveVizWorkflowId(app) {
  const preferred = preferredWorkflowHandle(app);
  if (preferred) {
    return preferred;
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

async function refreshRuntimeBinding(appId) {
  if (runtimeRefreshBusy) return;
  runtimeRefreshBusy = true;
  try {
    const response = await fetch(`${API}/api/apps/`);
    if (!response.ok) return;
    const data = await response.json();
    const nextApp = ensureArray(data.apps).find(app => app.app_id === appId) || null;
    if (!nextApp) return;

    const previousWorkflowId = vizState.workflowId;
    currentApp = nextApp;
    byId('toolbar-deployment-status').textContent = appStatusLabel(currentApp.deployment_status);
    byId('toolbar-run-status').textContent = appStatusLabel(currentApp.run_status);
    byId('toolbar-workflow-handle').textContent = preferredWorkflowHandle(currentApp) || '—';
    renderExecutionSummary(vizState.snapshot?.execution || {});

    const nextWorkflowId = await resolveVizWorkflowId(currentApp);
    if (nextWorkflowId !== previousWorkflowId) {
      await bindVizForCurrentApp();
    }
  } catch (error) {
    console.warn('refresh runtime binding failed', error);
  } finally {
    runtimeRefreshBusy = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab-btn').forEach(button => {
    button.addEventListener('click', () => setActiveTab(button.dataset.tab));
  });

  const parts = window.location.pathname.split('/');
  const appId = parts[parts.length - 1] || getQueryParam('app_id');
  byId('app-id').textContent = appId;

  loadAppDetail(appId).then(() => {
    runtimeRefreshTimer = window.setInterval(() => {
      refreshRuntimeBinding(appId);
    }, 3000);
  }).catch(error => {
    byId('e-name').value = '';
    byId('e-task').value = '';
    byId('e-mode').value = 'adaptive';
    byId('e-skills').value = '';
    byId('toolbar-deployment-status').textContent = '—';
    byId('toolbar-run-status').textContent = '—';
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

  byId('refresh-workflow-trends').addEventListener('click', loadWorkflowTrends);
  byId('workflow-trend-range').addEventListener('change', loadWorkflowTrends);

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

  window.addEventListener('beforeunload', () => {
    if (runtimeRefreshTimer) {
      window.clearInterval(runtimeRefreshTimer);
      runtimeRefreshTimer = null;
    }
    closeVizSocket();
  });
});
