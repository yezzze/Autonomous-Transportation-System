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

let orchestrationCy = null;
let selectedOrchestrationAppId = null;
let prometheusMetricsLoaded = false;
let expandedPrometheusInstanceId = null;
let activePrometheusDetailRow = null;
let prometheusDetailRefreshTimer = null;
let prometheusAggregation = 'p95';
let prometheusMetricsRequestId = 0;

function switchTestTab(name) {
  document.querySelectorAll('[data-test-tab]').forEach(button => {
    const active = button.dataset.testTab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.getElementById('test-panel-instances').classList.toggle('active', name === 'instances');
  document.getElementById('test-panel-orchestration').classList.toggle('active', name === 'orchestration');
  document.getElementById('test-panel-prometheus').classList.toggle('active', name === 'prometheus');
  if (name === 'orchestration') loadOrchestrationApps();
  if (name === 'prometheus' && !prometheusMetricsLoaded) loadPrometheusMetrics();
  if (name !== 'prometheus') closePrometheusInstanceDetails();
}

function formatPrometheusNumber(value, digits = 3) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits});
}

function renderPrometheusMetrics(instances) {
  closePrometheusInstanceDetails();
  const body = document.getElementById('prometheus-metrics-body');
  body.replaceChildren();
  if (!instances.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="8">暂无运行中的 Agent 实例</td></tr>';
    return;
  }
  instances.forEach(instance => {
    const row = document.createElement('tr');
    const cells = [instance.agent_id || '—'];
    cells.forEach(value => {
      const cell = document.createElement('td');
      cell.textContent = value;
      row.appendChild(cell);
    });
    const status = document.createElement('td');
    status.appendChild(statusBadge(instance.status));
    row.appendChild(status);
    const instanceId = document.createElement('td');
    instanceId.className = 'test-instance-id';
    instanceId.textContent = instance.instance_id || '—';
    row.appendChild(instanceId);
    const values = [
      instance.total_calls == null ? '—' : `${formatPrometheusNumber(instance.total_calls, 0)} 次`,
      instance.queue_wait_p95_seconds == null ? '—' : `${formatPrometheusNumber(instance.queue_wait_p95_seconds * 1000, 2)} ms`,
      instance.execution_p95_seconds == null ? '—' : `${formatPrometheusNumber(instance.execution_p95_seconds * 1000, 2)} ms`,
      instance.server_total_p95_seconds == null ? '—' : `${formatPrometheusNumber(instance.server_total_p95_seconds * 1000, 2)} ms`,
    ];
    values.forEach(value => {
      const cell = document.createElement('td');
      cell.className = 'prometheus-metric-value';
      cell.textContent = value;
      row.appendChild(cell);
    });
    const operation = document.createElement('td');
    const details = document.createElement('button');
    details.type = 'button';
    details.className = 'btn btn-primary btn-sm prometheus-instance-details-button';
    details.textContent = '查看详情';
    details.setAttribute('aria-expanded', 'false');
    details.addEventListener('click', () => openPrometheusInstanceDetails(instance, row, details));
    operation.appendChild(details);
    row.appendChild(operation);
    body.appendChild(row);
  });
}

function closePrometheusInstanceDetails() {
  if (prometheusDetailRefreshTimer !== null) {
    window.clearInterval(prometheusDetailRefreshTimer);
    prometheusDetailRefreshTimer = null;
  }
  if (activePrometheusDetailRow) activePrometheusDetailRow.remove();
  activePrometheusDetailRow = null;
  expandedPrometheusInstanceId = null;
  document.querySelectorAll('.prometheus-instance-details-button').forEach(button => {
    button.textContent = '查看详情';
    button.setAttribute('aria-expanded', 'false');
  });
}

const PROMETHEUS_INSTANCE_CHARTS = [
  ['total_calls', '累计调用次数', 1],
  ['queue_wait_p95', '排队等待', 1000],
  ['execution_p95', '执行耗时', 1000],
  ['server_total_p95', '服务端总耗时', 1000],
];

function prometheusAggregationLabel() {
  return prometheusAggregation === 'average' ? '平均值' : 'p95';
}

function updatePrometheusAggregationHeadings() {
  const suffix = prometheusAggregationLabel();
  document.getElementById('prometheus-queue-heading').textContent = `排队等待 ${suffix}`;
  document.getElementById('prometheus-execution-heading').textContent = `执行耗时 ${suffix}`;
  document.getElementById('prometheus-server-heading').textContent = `服务端总耗时 ${suffix}`;
}

async function openPrometheusInstanceDetails(instance, parentRow, trigger) {
  if (expandedPrometheusInstanceId === instance.instance_id) {
    closePrometheusInstanceDetails();
    return;
  }
  closePrometheusInstanceDetails();
  expandedPrometheusInstanceId = instance.instance_id;
  trigger.textContent = '收起';
  trigger.setAttribute('aria-expanded', 'true');

  const detailRow = document.createElement('tr');
  detailRow.className = 'prometheus-instance-detail-row';
  const cell = document.createElement('td');
  cell.colSpan = 8;
  const panel = document.createElement('div');
  panel.className = 'prometheus-instance-detail';
  const heading = document.createElement('div');
  heading.className = 'prometheus-instance-detail-heading';
  heading.textContent = `${instance.agent_id || 'Agent'} · ${instance.instance_id} · 最近 1 小时`;
  const grid = document.createElement('div');
  grid.className = 'prometheus-instance-chart-grid';
  const targets = {};
  PROMETHEUS_INSTANCE_CHARTS.forEach(([key, title]) => {
    const card = document.createElement('div');
    card.className = 'prometheus-instance-chart-card';
    const header = document.createElement('div');
    header.className = 'prometheus-chart-header';
    const strong = document.createElement('strong');
    strong.textContent = key === 'total_calls' ? title : `${title} ${prometheusAggregationLabel()}（ms）`;
    const state = document.createElement('span');
    state.className = 'test-call-state';
    state.textContent = '加载中...';
    header.append(strong, state);
    const legend = document.createElement('div');
    legend.className = 'prometheus-chart-legend';
    const chart = document.createElement('div');
    chart.className = 'prometheus-chart prometheus-instance-chart';
    const tooltip = document.createElement('div');
    tooltip.className = 'prometheus-chart-tooltip';
    tooltip.hidden = true;
    card.append(header, legend, chart, tooltip);
    grid.appendChild(card);
    targets[key] = {panel: card, chart, legend, tooltip, state, axisRange: null};
  });
  panel.append(heading, grid);
  cell.appendChild(panel);
  detailRow.appendChild(cell);
  parentRow.after(detailRow);
  activePrometheusDetailRow = detailRow;

  let refreshing = false;
  let hasLoaded = false;
  const refreshDetails = async () => {
    if (refreshing || expandedPrometheusInstanceId !== instance.instance_id) return;
    refreshing = true;
    Object.values(targets).forEach(target => {
      target.state.textContent = hasLoaded ? '刷新中...' : '加载中...';
    });
    try {
      const response = await fetch(
        `${API}/tests/prometheus/agent-metrics/${encodeURIComponent(instance.instance_id)}/history?aggregation=${encodeURIComponent(prometheusAggregation)}`,
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      if (expandedPrometheusInstanceId !== instance.instance_id || !detailRow.isConnected) return;
      const updateTime = new Date().toLocaleTimeString('zh-CN');
      PROMETHEUS_INSTANCE_CHARTS.forEach(([key, _title, valueMultiplier]) => {
        renderPrometheusChart(
          data.metrics?.[key] || {result: []},
          targets[key],
          {
            valueMultiplier,
            xMin: data.query_range?.start,
            xMax: data.query_range?.end,
            sampleStep: data.query_range?.step,
          },
        );
        targets[key].state.textContent = (data.unavailable_metrics || []).includes(key)
          ? '指标暂不可用'
          : `更新于 ${updateTime}`;
      });
      hasLoaded = true;
    } catch (error) {
      if (expandedPrometheusInstanceId !== instance.instance_id || !detailRow.isConnected) return;
      Object.values(targets).forEach(target => {
        target.state.textContent = `刷新失败：${error.message}`;
        if (!hasLoaded) target.chart.innerHTML = '<div class="empty-state">无法加载趋势数据</div>';
      });
    } finally {
      refreshing = false;
    }
  };
  refreshDetails();
  prometheusDetailRefreshTimer = window.setInterval(refreshDetails, 15000);
}

async function loadPrometheusMetrics() {
  const requestId = ++prometheusMetricsRequestId;
  const refresh = document.getElementById('refresh-prometheus-metrics');
  const hint = document.getElementById('prometheus-metrics-refresh-hint');
  refresh.disabled = true;
  clearAlert();
  try {
    const response = await fetch(`${API}/tests/prometheus/agent-metrics?aggregation=${encodeURIComponent(prometheusAggregation)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    if (requestId !== prometheusMetricsRequestId) return;
    renderPrometheusMetrics(data.instances || []);
    prometheusMetricsLoaded = true;
    const unavailable = data.unavailable_metrics || [];
    hint.textContent = unavailable.length
      ? `上次更新 ${new Date().toLocaleTimeString('zh-CN')}，部分指标不可用`
      : `上次更新 ${new Date().toLocaleTimeString('zh-CN')}`;
    setServerStatus(true);
  } catch (error) {
    if (requestId !== prometheusMetricsRequestId) return;
    renderPrometheusMetrics([]);
    showAlert('error', `加载 Prometheus 指标失败：${error.message}`);
  } finally {
    if (requestId === prometheusMetricsRequestId) refresh.disabled = false;
  }
}

function renderPrometheusQueryResult(data) {
  const container = document.getElementById('prometheus-query-result');
  const body = document.getElementById('prometheus-query-body');
  body.replaceChildren();
  let results = data.result || [];
  if (data.result_type === 'scalar' || data.result_type === 'string') {
    results = [{metric: {}, value: results}];
  }
  if (!results.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="3">查询成功，但 Prometheus 没有返回数据</td></tr>';
  } else {
    results.forEach(item => {
      const sample = item.value || [];
      const row = document.createElement('tr');
      const labels = document.createElement('td');
      labels.className = 'prometheus-labels';
      labels.textContent = JSON.stringify(item.metric || {}, null, 2);
      const value = document.createElement('td');
      value.className = 'prometheus-metric-value';
      value.textContent = sample[1] ?? '—';
      const timestamp = document.createElement('td');
      timestamp.textContent = Number.isFinite(Number(sample[0]))
        ? new Date(Number(sample[0]) * 1000).toLocaleString('zh-CN')
        : '—';
      row.append(labels, value, timestamp);
      body.appendChild(row);
    });
  }
  container.hidden = false;
}

const PROMETHEUS_CHART_COLORS = ['#2563eb', '#dc2626', '#16a34a', '#9333ea', '#ea580c', '#0891b2', '#4f46e5', '#be123c'];
const PROMETHEUS_DISPLAY_DIGITS = 6;

function prometheusSeriesLabel(metric) {
  const entries = Object.entries(metric || {}).sort(([left], [right]) => left.localeCompare(right));
  return entries.length ? entries.map(([key, value]) => `${key}="${value}"`).join(', ') : 'result';
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
  return element;
}

function prometheusSeriesSample(item, targetTime) {
  const points = item.points;
  if (!points.length || targetTime < points[0][0] || targetTime > points[points.length - 1][0]) {
    return null;
  }
  if (points.length === 1) return points[0];
  let low = 0;
  let high = points.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (points[middle][0] < targetTime) low = middle + 1;
    else high = middle - 1;
  }
  if (low < points.length && points[low][0] === targetTime) return points[low];
  if (low === 0) return points[0];
  if (low >= points.length) return points[points.length - 1];
  const left = points[low - 1];
  const right = points[low];
  return targetTime - left[0] <= right[0] - targetTime ? left : right;
}

function prometheusNiceTickStep(valueRange, tickCount = 10) {
  if (!Number.isFinite(valueRange) || valueRange <= 0) return 1;
  const roughStep = valueRange / tickCount;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const fraction = roughStep / magnitude;
  const niceFractions = [1, 2, 2.5, 3, 5, 10];
  const niceFraction = niceFractions.find(candidate => candidate >= fraction) || 10;
  return niceFraction * magnitude;
}

function prometheusTickDigits(tickStep) {
  if (!Number.isFinite(tickStep) || tickStep <= 0) return 3;
  return Math.min(12, Math.max(0, -Math.floor(Math.log10(tickStep))));
}

function prometheusAxisValue(value) {
  // 纵轴范围与 tooltip 的显示精度保持一致，避免不可见的浮点噪声被自动缩放放大。
  return Number(Number(value).toFixed(PROMETHEUS_DISPLAY_DIGITS));
}

function prometheusSeriesSegments(points, sampleStep) {
  if (!points.length) return [];
  const gaps = points.slice(1)
    .map((point, index) => point[0] - points[index][0])
    .filter(gap => gap > 0)
    .sort((left, right) => left - right);
  const inferredStep = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0;
  const expectedStep = Number(sampleStep) > 0 ? Number(sampleStep) : inferredStep;
  const gapThreshold = expectedStep > 0 ? expectedStep * 2.5 : Number.POSITIVE_INFINITY;
  const segments = [[points[0]]];
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1];
    const point = points[index];
    if (point[0] - previous[0] > gapThreshold) segments.push([]);
    segments[segments.length - 1].push(point);
  }
  return segments;
}

function renderPrometheusChart(data, elements = {}, options = {}) {
  const panel = elements.panel || document.getElementById('prometheus-chart-panel');
  const chart = elements.chart || document.getElementById('prometheus-chart');
  const legend = elements.legend || document.getElementById('prometheus-chart-legend');
  const tooltip = elements.tooltip || document.getElementById('prometheus-chart-tooltip');
  panel.hidden = false;
  chart.replaceChildren();
  legend.replaceChildren();
  tooltip.hidden = true;

  const valueMultiplier = options.valueMultiplier || 1;
  const series = (data.result || []).map((item, index) => ({
    label: prometheusSeriesLabel(item.metric),
    color: PROMETHEUS_CHART_COLORS[index % PROMETHEUS_CHART_COLORS.length],
    points: (item.values || []).map(sample => [Number(sample[0]), Number(sample[1]) * valueMultiplier])
      .filter(point => Number.isFinite(point[0]) && Number.isFinite(point[1])),
  })).filter(item => item.points.length);
  if (!series.length) {
    chart.innerHTML = '<div class="empty-state">当前时间范围内没有可绘制的数据</div>';
    return;
  }

  series.forEach(item => {
    const legendItem = document.createElement('span');
    legendItem.className = 'prometheus-legend-item';
    const swatch = document.createElement('span');
    swatch.className = 'prometheus-legend-swatch';
    swatch.style.background = item.color;
    const label = document.createElement('span');
    label.textContent = item.label;
    legendItem.append(swatch, label);
    legend.appendChild(legendItem);
  });

  const width = 960;
  const height = 320;
  const padding = {left: 72, right: 20, top: 16, bottom: 38};
  const timestamps = series.flatMap(item => item.points.map(point => point[0]));
  const values = series.flatMap(item => item.points.map(point => point[1]));
  const axisValues = values.map(prometheusAxisValue);
  const requestedMinTime = Number(options.xMin ?? data.query_range?.start);
  const requestedMaxTime = Number(options.xMax ?? data.query_range?.end);
  const minTime = Number.isFinite(requestedMinTime) ? requestedMinTime : Math.min(...timestamps);
  const maxTime = Number.isFinite(requestedMaxTime) ? requestedMaxTime : Math.max(...timestamps);
  const dataMinValue = Math.min(...axisValues);
  const dataMaxValue = Math.max(...axisValues);
  const valueRange = dataMaxValue - dataMinValue;
  const fallbackRange = Math.abs(dataMaxValue || 1) * 0.1;
  let tickStep = prometheusNiceTickStep(valueRange || fallbackRange, 10);
  let alignmentTolerance = tickStep * 1e-9;
  const alignedDataMin = valueRange ? dataMinValue : dataMinValue - tickStep * 5;
  let minValue = Math.floor((alignedDataMin + alignmentTolerance) / tickStep) * tickStep;
  let maxValue = minValue + tickStep * 10;
  while (maxValue < dataMaxValue - alignmentTolerance) {
    tickStep = prometheusNiceTickStep(tickStep * 10 * (1 + 1e-9), 10);
    alignmentTolerance = tickStep * 1e-9;
    const retryDataMin = valueRange ? dataMinValue : dataMinValue - tickStep * 5;
    minValue = Math.floor((retryDataMin + alignmentTolerance) / tickStep) * tickStep;
    maxValue = minValue + tickStep * 10;
  }
  const previousAxisRange = elements.axisRange;
  if (
    previousAxisRange
    && dataMinValue >= previousAxisRange.min - previousAxisRange.step * 1e-9
    && dataMaxValue <= previousAxisRange.max + previousAxisRange.step * 1e-9
  ) {
    minValue = previousAxisRange.min;
    maxValue = previousAxisRange.max;
    tickStep = previousAxisRange.step;
    alignmentTolerance = tickStep * 1e-9;
  } else if (elements && Object.keys(elements).length) {
    elements.axisRange = {min: minValue, max: maxValue, step: tickStep};
  }
  const tickDigits = prometheusTickDigits(tickStep);
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const x = timestamp => padding.left + ((timestamp - minTime) / (maxTime - minTime || 1)) * plotWidth;
  const y = value => padding.top + (1 - (value - minValue) / (maxValue - minValue)) * plotHeight;
  const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'xMidYMid meet'});

  for (let index = 0; index <= 10; index += 1) {
    const gridY = padding.top + (plotHeight * index) / 10;
    svg.appendChild(svgElement('line', {x1: padding.left, y1: gridY, x2: width - padding.right, y2: gridY, stroke: '#e2e8f0'}));
    const label = svgElement('text', {x: padding.left - 8, y: gridY + 4, 'text-anchor': 'end', fill: '#64748b', 'font-size': 11});
    const tickValue = maxValue - tickStep * index;
    label.textContent = formatPrometheusNumber(Math.abs(tickValue) < alignmentTolerance ? 0 : tickValue, tickDigits);
    svg.appendChild(label);
  }
  const fiveMinutesSeconds = 5 * 60;
  const firstTimeTick = Math.ceil(minTime / fiveMinutesSeconds) * fiveMinutesSeconds;
  const lastTimeTick = Math.floor(maxTime / fiveMinutesSeconds) * fiveMinutesSeconds;
  const timeTickCount = Math.max(0, Math.floor((lastTimeTick - firstTimeTick) / fiveMinutesSeconds) + 1);
  const labelEvery = Math.max(1, Math.ceil(timeTickCount / 12));
  for (let index = 0; index < timeTickCount; index += 1) {
    const timestamp = firstTimeTick + index * fiveMinutesSeconds;
    const gridX = x(timestamp);
    svg.appendChild(svgElement('line', {
      x1: gridX,
      y1: padding.top,
      x2: gridX,
      y2: height - padding.bottom,
      stroke: '#94a3b8',
      'stroke-width': 1,
      'stroke-opacity': 0.28,
      'vector-effect': 'non-scaling-stroke',
    }));
    if (index % labelEvery !== 0 && index !== timeTickCount - 1) continue;
    const label = svgElement('text', {
      x: gridX,
      y: height - 12,
      'text-anchor': 'middle',
      fill: '#64748b',
      'font-size': 11,
    });
    label.textContent = new Date(timestamp * 1000).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    });
    svg.appendChild(label);
  }
  svg.appendChild(svgElement('rect', {
    x: padding.left,
    y: padding.top,
    width: plotWidth,
    height: plotHeight,
    fill: 'none',
    stroke: '#94a3b8',
    'stroke-width': 1,
    'stroke-opacity': 0.52,
    'vector-effect': 'non-scaling-stroke',
  }));
  series.forEach(item => {
    const sampleStep = options.sampleStep ?? data.query_range?.step;
    prometheusSeriesSegments(item.points, sampleStep).forEach(segment => {
      if (segment.length === 1) {
        svg.appendChild(svgElement('circle', {
          cx: x(segment[0][0]),
          cy: y(segment[0][1]),
          r: 2,
          fill: item.color,
        }));
        return;
      }
      const points = segment.map(point => `${x(point[0])},${y(point[1])}`).join(' ');
      svg.appendChild(svgElement('polyline', {
        points,
        fill: 'none',
        stroke: item.color,
        'stroke-width': 2,
        'vector-effect': 'non-scaling-stroke',
      }));
    });
  });

  const hoverLine = svgElement('line', {y1: padding.top, y2: height - padding.bottom, stroke: '#64748b', 'stroke-dasharray': '4 3', visibility: 'hidden'});
  svg.appendChild(hoverLine);
  const overlay = svgElement('rect', {x: padding.left, y: padding.top, width: plotWidth, height: plotHeight, fill: 'transparent'});
  overlay.addEventListener('mousemove', event => {
    const bounds = svg.getBoundingClientRect();
    const rawSvgX = ((event.clientX - bounds.left) / bounds.width) * width;
    const lineX = Math.max(padding.left, Math.min(width - padding.right, rawSvgX));
    const targetTime = minTime + ((lineX - padding.left) / plotWidth) * (maxTime - minTime);
    const intersections = series.map(item => ({
      item,
      sample: prometheusSeriesSample(item, targetTime),
    })).filter(intersection => intersection.sample !== null);
    hoverLine.setAttribute('x1', lineX);
    hoverLine.setAttribute('x2', lineX);
    hoverLine.setAttribute('visibility', 'visible');
    tooltip.replaceChildren();
    const time = document.createElement('div');
    time.className = 'prometheus-tooltip-time';
    time.textContent = new Date(targetTime * 1000).toLocaleString('zh-CN');
    tooltip.appendChild(time);
    intersections.forEach(intersection => {
      const row = document.createElement('div');
      row.className = 'prometheus-tooltip-row';
      const swatch = document.createElement('span');
      swatch.className = 'prometheus-tooltip-swatch';
      swatch.style.background = intersection.item.color;
      const content = document.createElement('span');
      content.textContent = `${intersection.item.label}: ${formatPrometheusNumber(intersection.sample[1], PROMETHEUS_DISPLAY_DIGITS)}`;
      row.append(swatch, content);
      tooltip.appendChild(row);
    });
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(8, Math.min(event.clientX - panel.getBoundingClientRect().left + 12, panel.clientWidth - tooltip.offsetWidth - 12))}px`;
    tooltip.style.top = `${event.clientY - panel.getBoundingClientRect().top + 12}px`;
  });
  overlay.addEventListener('mouseleave', () => {
    hoverLine.setAttribute('visibility', 'hidden');
    tooltip.hidden = true;
  });
  svg.appendChild(overlay);
  chart.appendChild(svg);
}

async function loadPrometheusChart(query) {
  const chartState = document.getElementById('prometheus-chart-state');
  const panel = document.getElementById('prometheus-chart-panel');
  panel.hidden = false;
  chartState.textContent = '正在加载趋势数据';
  const response = await fetch(`${API}/tests/prometheus/query-range`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      query,
      range_seconds: Number(document.getElementById('prometheus-range').value),
    }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  renderPrometheusChart(data);
  chartState.textContent = data.warnings?.length ? `趋势加载完成（${data.warnings.length} 条警告）` : '趋势加载完成';
}

async function runPrometheusQuery(event) {
  event.preventDefault();
  const query = document.getElementById('prometheus-query').value.trim();
  const trigger = document.getElementById('run-prometheus-query');
  const state = document.getElementById('prometheus-query-state');
  if (!query) return;
  clearAlert();
  trigger.disabled = true;
  trigger.textContent = '查询中...';
  state.textContent = '正在等待 Prometheus 响应';
  try {
    const response = await fetch(`${API}/tests/prometheus/query`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    renderPrometheusQueryResult(data);
    state.textContent = data.warnings?.length ? `即时查询完成（${data.warnings.length} 条警告）` : '即时查询完成';
    try {
      await loadPrometheusChart(query);
    } catch (chartError) {
      document.getElementById('prometheus-chart-state').textContent = `趋势加载失败：${chartError.message}`;
    }
    setServerStatus(true);
  } catch (error) {
    state.textContent = '查询失败';
    showAlert('error', `Prometheus 查询失败：${error.message}`);
  } finally {
    trigger.disabled = false;
    trigger.textContent = '查询';
  }
}

function renderOrchestrationApps(apps) {
  const body = document.getElementById('orchestration-apps-body');
  body.replaceChildren();
  if (!apps.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">暂无应用</td></tr>';
    return;
  }
  apps.forEach(application => {
    const row = document.createElement('tr');
    const name = document.createElement('td');
    const strong = document.createElement('strong');
    strong.textContent = application.name || '—';
    name.appendChild(strong);
    const appId = document.createElement('td');
    appId.className = 'test-instance-id';
    appId.textContent = application.app_id || '—';
    const status = document.createElement('td');
    status.appendChild(statusBadge(application.status));
    const handle = document.createElement('td');
    handle.className = 'test-instance-id';
    handle.textContent = application.workflow_handle || '—';
    const operation = document.createElement('td');
    const details = document.createElement('button');
    details.type = 'button';
    details.className = 'btn btn-primary btn-sm';
    details.textContent = '查看详情';
    details.addEventListener('click', () => showApplicationOrchestrationDetails(application));
    operation.appendChild(details);
    row.append(name, appId, status, handle, operation);
    body.appendChild(row);
  });
}

function showApplicationOrchestrationDetails(application) {
  selectedOrchestrationAppId = application.app_id;
  const guidance = application.guidance_file || {};
  const card = document.getElementById('application-orchestration-card');
  document.getElementById('application-orchestration-name').textContent =
    `${application.name || '未命名应用'}（${application.app_id || '—'}）`;
  document.getElementById('application-task-description').value = guidance.task_description || '';
  document.getElementById('application-skills-content').value = guidance.skills_content || '';
  document.getElementById('application-orchestration-plan-state').textContent = '';
  card.hidden = false;
  card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

async function loadOrchestrationApps() {
  const refresh = document.getElementById('refresh-orchestration-apps');
  refresh.disabled = true;
  try {
    const response = await fetch(`${API}/api/apps/`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    renderOrchestrationApps(data.apps || []);
    document.getElementById('orchestration-apps-refresh-hint').textContent =
      `上次更新 ${new Date().toLocaleTimeString('zh-CN')}`;
    setServerStatus(true);
  } catch (error) {
    renderOrchestrationApps([]);
    showAlert('error', `加载应用失败：${error.message}`);
    setServerStatus(false);
  } finally {
    refresh.disabled = false;
  }
}

function renderOrchestrationResult(data) {
  const visualization = window.OrchestrationVisualization;
  visualization.renderPipeline(document.getElementById('test-pipeline-flow'), data.orchestration || {});
  const taskGraph = data.task_graph || data.topology || {};
  document.getElementById('test-execution-plan-json').textContent =
    JSON.stringify(data.execution_plan || [], null, 2);
  document.getElementById('test-task-graph-json').textContent =
    JSON.stringify(taskGraph, null, 2);
  const canvas = document.getElementById('test-cy');
  const empty = document.getElementById('test-topology-empty');
  const hasNodes = Array.isArray(taskGraph.nodes) && taskGraph.nodes.length > 0;
  canvas.classList.toggle('show', hasNodes);
  empty.style.display = hasNodes ? 'none' : 'block';
  empty.textContent = hasNodes ? '' : '未生成任务图';
  if (!hasNodes) {
    if (orchestrationCy) orchestrationCy.elements().remove();
    return;
  }
  if (!orchestrationCy) orchestrationCy = visualization.createTopology(canvas);
  visualization.renderTopology(orchestrationCy, taskGraph);
  orchestrationCy.resize();
  orchestrationCy.fit(undefined, 30);
}

async function requestOrchestrationPlan(payload, trigger, stateElementId = 'orchestration-plan-state') {
  const state = document.getElementById(stateElementId);
  clearAlert();
  trigger.disabled = true;
  const originalText = trigger.textContent;
  trigger.textContent = '编排中...';
  state.textContent = '正在生成执行计划';
  try {
    const response = await fetch(`${API}/tests/orchestration/plan`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    renderOrchestrationResult(data);
    state.textContent = `编排完成，共 ${(data.task_graph || data.topology || {}).total || 0} 个任务`;
  } catch (error) {
    state.textContent = '编排失败';
    showAlert('error', `编排失败：${error.message}`);
  } finally {
    trigger.disabled = false;
    trigger.textContent = originalText;
  }
}

document.querySelectorAll('[data-test-tab]').forEach(button => {
  button.addEventListener('click', () => switchTestTab(button.dataset.testTab));
});
document.getElementById('refresh-orchestration-apps').addEventListener('click', loadOrchestrationApps);
document.getElementById('plan-application-orchestration').addEventListener('click', event => {
  if (!selectedOrchestrationAppId) {
    showAlert('error', '请先从应用列表中选择应用');
    return;
  }
  requestOrchestrationPlan(
    {app_id: selectedOrchestrationAppId},
    event.currentTarget,
    'application-orchestration-plan-state',
  );
});
document.getElementById('plan-custom-orchestration').addEventListener('click', event => {
  const taskDescription = document.getElementById('orchestration-task-description').value.trim();
  if (!taskDescription) {
    showAlert('error', '请填写任务描述');
    document.getElementById('orchestration-task-description').focus();
    return;
  }
  requestOrchestrationPlan({
    task_description: taskDescription,
    skills_content: document.getElementById('orchestration-skills-content').value,
  }, event.currentTarget);
});
document.getElementById('refresh-instances').addEventListener('click', loadInstances);
document.getElementById('refresh-prometheus-metrics').addEventListener('click', loadPrometheusMetrics);
document.querySelectorAll('input[name="prometheus-aggregation"]').forEach(input => {
  input.addEventListener('change', event => {
    if (!event.target.checked || event.target.value === prometheusAggregation) return;
    prometheusAggregation = event.target.value;
    updatePrometheusAggregationHeadings();
    closePrometheusInstanceDetails();
    loadPrometheusMetrics();
  });
});
document.getElementById('prometheus-query-form').addEventListener('submit', runPrometheusQuery);
loadInstances();
