(function (global) {
  const STATUS_COLORS = {
    pending: {bg: '#f1f5f9', border: '#64748b'},
    running: {bg: '#dbeafe', border: '#2563eb'},
    completed: {bg: '#dcfce7', border: '#16a34a'},
    failed: {bg: '#fee2e2', border: '#dc2626'},
  };
  const AGENT_TYPE_COLORS = {
    business: '#dbeafe',
    resource: '#fef3c7',
  };

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function renderPipeline(container, orchestration) {
    container.replaceChildren();
    const items = list(orchestration && orchestration.pipeline_topology);
    if (!items.length) {
      container.innerHTML = '<span class="empty-state">无固定 Pipeline，使用 Planner 动态规划</span>';
      return;
    }

    const stepElement = step => {
      const element = document.createElement('div');
      element.className = 'viz-pipe-step';
      element.textContent = step.step || step.capability || step.description || step.agent_id || '';
      return element;
    };

    let index = 0;
    while (index < items.length) {
      const item = items[index] || {};
      const parallelGroup = !Array.isArray(item) && (item.parallel_group || '');
      if (Array.isArray(item) || parallelGroup) {
        const wrapper = document.createElement('div');
        wrapper.className = 'viz-pipe-parallel';
        if (Array.isArray(item)) {
          item.filter(step => step && typeof step === 'object').forEach(step => wrapper.appendChild(stepElement(step)));
          index += 1;
        } else {
          while (index < items.length && items[index] && items[index].parallel_group === parallelGroup) {
            wrapper.appendChild(stepElement(items[index]));
            index += 1;
          }
        }
        container.appendChild(wrapper);
      } else {
        container.appendChild(stepElement(item));
        index += 1;
      }
      if (index < items.length) {
        const arrow = document.createElement('span');
        arrow.className = 'viz-pipe-arrow';
        arrow.textContent = '→';
        container.appendChild(arrow);
      }
    }
  }

  function createTopology(container) {
    if (typeof global.cytoscape === 'undefined') return null;
    return global.cytoscape({
      container,
      elements: [],
      style: [
        {selector: 'node', style: {
          'background-color': 'data(bgcolor)', 'border-color': 'data(bordercolor)', 'border-width': 2,
          label: 'data(label)', color: '#1f2937', 'font-size': 11, 'text-valign': 'center',
          'text-halign': 'center', 'text-wrap': 'wrap', 'text-max-width': '120px',
          shape: 'round-rectangle', width: 140, height: 50, padding: '6px',
        }},
        {selector: 'node.current', style: {'border-width': 4, 'border-color': '#2563eb'}},
        {selector: 'node.platform', style: {
          shape: 'round-tag', 'background-color': '#f8fafc', 'border-color': 'data(bordercolor)',
          'border-style': 'dashed', color: '#334155', 'font-size': 12, 'font-weight': 700,
          padding: '10px', 'text-valign': 'top', 'text-margin-y': -8,
        }},
        {selector: 'edge', style: {
          width: 2, 'line-color': '#94a3b8', 'target-arrow-color': '#94a3b8',
          'target-arrow-shape': 'triangle', 'curve-style': 'bezier',
        }},
        {selector: 'edge.parallel_start, edge.parallel_group', style: {
          'line-color': '#7c3aed', 'target-arrow-color': '#7c3aed', 'line-style': 'dashed',
        }},
        {selector: 'edge.active', style: {
          'line-color': '#2563eb', 'target-arrow-color': '#2563eb', width: 3,
        }},
      ],
      layout: {name: 'dagre', rankDir: 'LR', nodeSep: 50, rankSep: 90},
      wheelSensitivity: 0.2,
    });
  }

  function renderTopology(cy, topology) {
    if (!cy) return;
    const data = topology || {};
    const platforms = list(data.platforms);
    const nodes = list(data.nodes);
    const elements = [];
    platforms.forEach(platform => elements.push({
      data: {
        id: `platform_${platform.key}`,
        label: `${platform.platform === 'local' ? '🏠 本机' : '☁️ 远端'} ${platform.key}`,
        bordercolor: platform.platform === 'local' ? '#3b82f6' : '#8b5cf6',
      },
      classes: 'platform',
    }));
    nodes.forEach(node => {
      const color = STATUS_COLORS[node.status] || STATUS_COLORS.pending;
      const key = `${node.ip || '-'}:${node.port || '-'}`;
      elements.push({
        data: {
          id: node.id,
          parent: platforms.some(platform => platform.key === key) ? `platform_${key}` : undefined,
          label: `${node.title || node.id}\n[${node.agent_id || '-'}]`,
          bgcolor: AGENT_TYPE_COLORS[node.agent_type] || AGENT_TYPE_COLORS.business,
          bordercolor: color.border,
        },
        classes: node.is_current ? 'current' : '',
      });
    });
    list(data.edges).forEach(edge => elements.push({
      data: {id: `${edge.from}->${edge.to}`, source: edge.from, target: edge.to},
      classes: `${edge.type || ''}${nodes.some(node => node.id === edge.to && node.is_current) ? ' active' : ''}`.trim(),
    }));
    cy.elements().remove();
    cy.add(elements);
    cy.layout({name: 'dagre', rankDir: 'LR', nodeSep: 40, rankSep: 95, fit: true, animate: false}).run();
  }

  global.OrchestrationVisualization = {renderPipeline, createTopology, renderTopology};
})(window);
