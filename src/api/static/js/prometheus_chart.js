(function () {
  'use strict';

  function formatPrometheusNumber(value, digits = 3) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    return Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits});
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

  window.renderPrometheusChart = renderPrometheusChart;
  window.prometheusSeriesSegments = prometheusSeriesSegments;
})();

