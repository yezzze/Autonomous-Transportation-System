// Simple front-end for App Details page
const API = '';

let currentApp = null;

function getQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  return params.get(name);
}

function setActiveTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
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

function normalizeAgentViewUrl(url) {
  if (!url) {
    return '';
  }
  return String(url).trim();
}

function parsePipeline(skillsContent) {
  if (!skillsContent || !skillsContent.trim()) {
    return [];
  }

  const headingMatch = skillsContent.match(/^##\s+Pipeline\s*$/im);
  if (!headingMatch) {
    return [];
  }

  const afterHeading = skillsContent.slice(headingMatch.index + headingMatch[0].length);
  const nextHeadingMatch = afterHeading.match(/^##\s+/m);
  const section = (nextHeadingMatch ? afterHeading.slice(0, nextHeadingMatch.index) : afterHeading).trim();
  if (!section) {
    return [];
  }

  const lines = section
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line && !line.startsWith('#'));

  if (!lines.length) {
    return [];
  }

  const merged = [lines[0]];
  for (const line of lines.slice(1)) {
    if (line.startsWith('->')) {
      merged[merged.length - 1] += ` ${line}`;
    } else {
      break;
    }
  }

  const raw = merged[0].replace(/^->\s*/, '').split(/\s*->\s*/);
  const steps = [];

  for (const token of raw) {
    const trimmed = token.trim();
    if (!trimmed) {
      continue;
    }

    const groupMatch = trimmed.match(/^\[(.+)\]$/);
    if (groupMatch) {
      const group = groupMatch[1]
        .split(',')
        .map(item => item.trim())
        .filter(Boolean)
        .map(parsePipelineStep);
      if (group.length) {
        steps.push(group);
      }
      continue;
    }

    steps.push(parsePipelineStep(trimmed));
  }

  return steps.filter(Boolean);
}

function parsePipelineStep(token) {
  const [capabilityPart, ...rest] = String(token).split(':');
  const capability = capabilityPart.trim().toLowerCase();
  const description = rest.join(':').trim();
  return {
    capability,
    description,
    agent_id: `${capability}_agent_001`,
  };
}

function buildWorkflowSteps(app) {
  const guidance = app && app.guidance_file ? app.guidance_file : {};
  const parsed = parsePipeline(guidance.skills_content || '');
  if (parsed.length) {
    return parsed;
  }

  const agentsRequired = Array.isArray(guidance.agents_required) ? guidance.agents_required : [];
  return agentsRequired
    .filter(Boolean)
    .map(capability => ({
      capability: String(capability).trim().toLowerCase(),
      description: '',
      agent_id: `${String(capability).trim().toLowerCase()}_agent_001`,
    }));
}

function createWorkflowNode(step, index, mini = false) {
  const node = document.createElement('div');
  node.className = `workflow-node${mini ? ' mini' : ''}`;
  const title = step.description || step.capability || '节点';
  const agentId = step.agent_id || `${step.capability || 'agent'}_agent_001`;

  node.innerHTML = `
    <div class="workflow-node-header">
      <span class="workflow-node-index">${index}</span>
      <div style="flex:1">
        <div class="workflow-node-title">${escapeHtml(step.capability || 'agent')}</div>
        <div class="workflow-node-agent">${escapeHtml(agentId)}</div>
      </div>
    </div>
    <div class="workflow-node-desc">${escapeHtml(title || '自动生成的编排节点')}</div>
  `;
  return node;
}

function renderWorkflow(app) {
  const summaryHost = document.getElementById('workflow-summary');
  const canvas = document.getElementById('orchestrator');
  if (!summaryHost || !canvas) {
    return;
  }

  const guidance = app && app.guidance_file ? app.guidance_file : {};
  const steps = buildWorkflowSteps(app);
  const status = app?.status || '—';
  const workflowHandle = app?.workflow_handle || '—';
  const orchestrationMode = guidance.orchestration_mode || 'adaptive';
  const running = app?.status === 'running';

  summaryHost.innerHTML = [
    `<span class="workflow-pill">状态：${escapeHtml(status)}</span>`,
    `<span class="workflow-pill">模式：${escapeHtml(orchestrationMode)}</span>`,
    `<span class="workflow-pill">步骤数：${steps.length || 0}</span>`,
    `<span class="workflow-pill">workflow_handle：${escapeHtml(workflowHandle)}</span>`,
  ].join('');

  canvas.classList.toggle('running', running);
  canvas.innerHTML = '';

  if (!steps.length) {
    const empty = document.createElement('div');
    empty.className = 'workflow-empty';
    empty.textContent = '当前应用未提供可解析的 Pipeline，保存 Skills.md 后会自动生成工作流。';
    canvas.appendChild(empty);
    return;
  }

  const flow = document.createElement('div');
  flow.className = 'workflow-flow';

  steps.forEach((step, index) => {
    const stepNumber = index + 1;
    if (Array.isArray(step)) {
      const group = document.createElement('div');
      group.className = 'workflow-group';

      const label = document.createElement('div');
      label.className = 'workflow-group-label';
      label.textContent = `并行分支 ×${step.length}`;
      group.appendChild(label);

      const inner = document.createElement('div');
      inner.className = 'workflow-group-inner';
      step.forEach((item, childIndex) => {
        inner.appendChild(createWorkflowNode(item, `${stepNumber}.${childIndex + 1}`, true));
      });
      group.appendChild(inner);
      flow.appendChild(group);
    } else {
      flow.appendChild(createWorkflowNode(step, stepNumber));
    }

    if (index < steps.length - 1) {
      const arrow = document.createElement('div');
      arrow.className = 'workflow-arrow';
      arrow.textContent = '→';
      flow.appendChild(arrow);
    }
  });

  canvas.appendChild(flow);
}

function setLogicForm(app) {
  const guidance = app && app.guidance_file ? app.guidance_file : {};
  document.getElementById('e-name').value = app?.name || '';
  document.getElementById('e-task').value = guidance.task_description || '';
  document.getElementById('e-mode').value = guidance.orchestration_mode || 'adaptive';
  document.getElementById('e-skills').value = guidance.skills_content || '';
}

function setRuntimeInfo(app) {
  const info = [];
  info.push(`<div>名称：${escapeHtml(app?.name || '—')}</div>`);
  info.push(`<div>应用 ID：${escapeHtml(app?.app_id || '—')}</div>`);
  info.push(`<div>状态：${escapeHtml(app?.status || '—')}</div>`);
  info.push(`<div>workflow_handle：${escapeHtml(app?.workflow_handle || '—')}</div>`);
  document.getElementById('runtime-info').innerHTML = info.join('');

  renderWorkflow(app);

  const agentsHost = document.getElementById('agents-host');
  if (agentsHost) {
    agentsHost.innerHTML = '<div class="empty-state">正在加载智能体前端视图...</div>';
  }
}

function renderAgentViews(views) {
  const agentsHost = document.getElementById('agents-host');
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
    // ip: '192.168.49.2',
    ip: '10.112.136.44',
    // port: 30092,
    port: 9002,
    status: 'running',
    // frontend_url: 'http://192.168.49.2:30092',
    frontend_url: 'http://10.112.136.44:9002',
  };

  const agentsHost = document.getElementById('agents-host');
  if (agentsHost) {
    agentsHost.innerHTML = '<div class="empty-state">正在加载智能体前端视图...</div>';
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

async function loadAppDetail(appId) {
  const appsResponse = await fetch(`${API}/api/apps/`);

  const appsData = await appsResponse.json();
  currentApp = (appsData.apps || []).find(app => app.app_id === appId) || null;

  if (!currentApp) {
    throw new Error(`应用 ${appId} 不存在`);
  }

  setLogicForm(currentApp);
  setRuntimeInfo(currentApp);
  await loadAgentViews(appId);
}

document.addEventListener('DOMContentLoaded', () => {
  // tab clicks
  document.querySelectorAll('.tab-btn').forEach(b => b.addEventListener('click', () => setActiveTab(b.dataset.tab)));

  const parts = window.location.pathname.split('/');
  const appId = parts[parts.length-1] || getQueryParam('app_id');
  document.getElementById('app-id').textContent = appId;

  loadAppDetail(appId).catch(e => {
    document.getElementById('e-name').value = '';
    document.getElementById('e-task').value = '';
    document.getElementById('e-mode').value = 'adaptive';
    document.getElementById('e-skills').value = '';
    document.getElementById('runtime-info').textContent = `加载失败：${e.message}`;
    document.getElementById('agents-host').textContent = '加载失败';
  });

  // button handlers
  document.getElementById('save-logic').addEventListener('click', async () => {
    const name = document.getElementById('e-name').value.trim();
    const taskDescription = document.getElementById('e-task').value.trim();
    const orchestrationMode = document.getElementById('e-mode').value;
    const skillsMd = document.getElementById('e-skills').value.trim();

    if (!name || !taskDescription) {
      alert('请填写应用名称和任务描述');
      return;
    }

    try {
      const res = await fetch(`${API}/api/apps/${encodeURIComponent(appId)}`, {
        method: 'PATCH', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          name,
          task_description: taskDescription,
          orchestration_mode: orchestrationMode,
          skills_md: skillsMd,
        })
      });
      if (!res.ok) throw new Error((await res.json()).detail||'保存失败');
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
    } catch(e) { alert('保存失败：'+e.message); }
  });

  document.getElementById('btn-start')?.addEventListener('click', async () => {
    try {
      const res = await fetch(`${API}/api/apps/${encodeURIComponent(appId)}/start`, {method:'POST'});
      if (!res.ok) throw new Error((await res.json()).detail||'启动失败');
      alert('启动成功');
    } catch(e) { alert('启动失败：'+e.message); }
  });
  document.getElementById('btn-stop')?.addEventListener('click', async () => {
    try {
      const res = await fetch(`${API}/api/apps/${encodeURIComponent(appId)}/stop`, {method:'POST'});
      if (!res.ok) throw new Error((await res.json()).detail||'停止失败');
      alert('停止成功');
    } catch(e) { alert('停止失败：'+e.message); }
  });
  document.getElementById('btn-reload')?.addEventListener('click', () => location.reload());
});
