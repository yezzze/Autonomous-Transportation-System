(function () {
  "use strict";

  const STORAGE_KEY = "collab_model_library_v1";
  const VIEW_STATE_KEY = "collab_model_library_view_state_v1";
  const ROOT_LABEL = "自主式交通系统协同计算基础算法模型";
  const REPO_URL_PREFIX = "http://10.112.76.79:8080/#path=algo";
  const SCENE_HEADERS = [
    "车端-低级别自动驾驶（<L3）",
    "车端-高级别自动驾驶（≥L3）",
    "车路协同",
    "端边云协同"
  ];
  const SCENE_CLASS = {
    "车端-低级别自动驾驶（<L3）": "scene-low",
    "车端-高级别自动驾驶（≥L3）": "scene-high",
    "车路协同": "scene-road",
    "端边云协同": "scene-cloud"
  };
  const ORG_HEADERS = Array.from({ length: 9 }, (_, index) => `模型组织（${index + 1}级）`);
  const NODE_SIZE = {
    root: { width: 340, height: 86 },
    group: { width: 280, height: 78 },
    model: { width: 410, height: 122 }
  };
  const H_GAP = 430;
  const V_GAP = 146;

  const els = {
    sourceBadge: document.getElementById("sourceBadge"),
    modelCount: document.getElementById("modelCount"),
    sceneSummary: document.getElementById("sceneSummary"),
    searchInput: document.getElementById("searchInput"),
    excelInput: document.getElementById("excelInput"),
    importBtn: document.getElementById("importBtn"),
    expandBtn: document.getElementById("expandBtn"),
    collapseBtn: document.getElementById("collapseBtn"),
    fitBtn: document.getElementById("fitBtn"),
    resetBtn: document.getElementById("resetBtn"),
    svg: document.getElementById("treeCanvas"),
    viewport: document.getElementById("treeViewport"),
    emptyState: document.getElementById("emptyState"),
    toast: document.getElementById("toast")
  };

  const state = {
    models: [],
    tree: null,
    expanded: new Set(),
    activePath: new Set(),
    activeModelId: "",
    query: "",
    sourceName: "",
    isCustom: false,
    transform: { x: 80, y: 300, k: 1 },
    lastLayout: null,
    toastTimer: 0,
    pendingModelClickTimer: 0,
    lastModelClick: { id: "", time: 0 },
    pan: null
  };

  function clean(value) {
    return value == null ? "" : String(value).replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  }

  function normalizeSearch(value) {
    return clean(value).toLowerCase();
  }

  function normalizeRepoUrl(value) {
    const url = clean(value);
    if (!url || url === "#" || url === "无" || url === "暂无" || url === "待补充" || url === "代码链接待补充") return "";
    return url;
  }

  function generateRepoUrl(modelId) {
    const normalizedId = clean(modelId).toLowerCase();
    return normalizedId ? `${REPO_URL_PREFIX}${encodeURIComponent(normalizedId)}` : "";
  }

  function isModelIdQuery(query) {
    return /^[a-z0-9]+(?:-[a-z0-9]+)+$/i.test(query) && !/[\u4e00-\u9fff]/.test(query);
  }

  function normalizeModel(raw) {
    const id = clean(raw.id || raw["模型编号"] || raw["模型ID"] || raw["编号"]);
    const currentName = clean(raw.currentName || raw["模型名称"] || raw["模型名称（现）"] || raw["名称"]);
    const originalName = clean(raw.originalName || raw["模型名称（原）"]);
    const description = clean(raw.description || raw["模型功能描述"] || raw["模型简介"] || raw["功能描述"]);
    const path = Array.isArray(raw.path)
      ? raw.path.map(clean).filter(Boolean)
      : ORG_HEADERS.map((header) => clean(raw[header])).filter(Boolean);
    const scenes = Array.isArray(raw.scenes)
      ? raw.scenes.map(clean).filter(Boolean)
      : SCENE_HEADERS.filter((header) => clean(raw[header]));
    const repoLinks = window.MODEL_REPO_LINKS || {};
    const codeUrl = [
      raw["仓库入口URL"],
      raw.codeUrl,
      raw.repoUrl,
      raw["代码仓库链接"],
      raw["模型代码链接"],
      raw["仓库链接"],
      raw["URL"],
      raw["url"],
      repoLinks[id],
      generateRepoUrl(id)
    ].map(normalizeRepoUrl).find(Boolean) || "无";

    if (!id) return null;
    if (!currentName && !originalName && path.length === 0) return null;

    return {
      seq: clean(raw.seq || raw["序号"]),
      id,
      currentName: currentName || originalName || `${id}（待补充）`,
      originalName,
      description,
      scenes,
      path,
      codeUrl,
      searchText: normalizeSearch([id, currentName, originalName, description, path.join(" ")].join(" "))
    };
  }

  function loadPayload() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        const models = (parsed.models || []).map(normalizeModel).filter(Boolean);
        if (models.length) {
          return {
            models,
            sourceName: clean(parsed.sourceName) || "自定义数据",
            isCustom: true
          };
        }
      } catch (error) {
        console.warn("Failed to read imported model data", error);
      }
    }

    const defaults = window.DEFAULT_MODEL_LIBRARY || { sourceFile: "默认数据", models: [] };
    return {
      models: (defaults.models || []).map(normalizeModel).filter(Boolean),
      sourceName: defaults.sourceFile || "默认数据",
      isCustom: false
    };
  }

  function createTree(models) {
    let order = 0;
    const root = {
      key: "root",
      type: "root",
      label: ROOT_LABEL,
      depth: 0,
      children: [],
      parent: null,
      order: order++,
      modelCount: 0
    };

    models.forEach((model) => {
      let current = root;
      const path = model.path.length ? model.path : ["待补充模型", "信息缺失"];
      path.forEach((label, index) => {
        const key = `${current.key}/${index}:${label}`;
        let child = current.children.find((item) => item.key === key && item.type === "group");
        if (!child) {
          child = {
            key,
            type: "group",
            label,
            depth: current.depth + 1,
            children: [],
            parent: current,
            order: order++,
            modelCount: 0
          };
          current.children.push(child);
        }
        current = child;
      });
      current.children.push({
        key: `model:${model.id}`,
        type: "model",
        label: model.currentName,
        depth: current.depth + 1,
        children: [],
        parent: current,
        order: order++,
        model,
        modelCount: 1
      });
    });

    calculateCounts(root);
    return root;
  }

  function calculateCounts(node) {
    if (node.type === "model") {
      node.modelCount = 1;
      return 1;
    }
    node.modelCount = node.children.reduce((sum, child) => sum + calculateCounts(child), 0);
    return node.modelCount;
  }

  function setInitialExpansion(node) {
    state.expanded.clear();
    walkTree(node, (item) => {
      if (item.type !== "model" && item.depth < 2) state.expanded.add(item.key);
    });
  }

  function saveViewState() {
    if (!state.tree) return;
    localStorage.setItem(VIEW_STATE_KEY, JSON.stringify({
      expanded: Array.from(state.expanded),
      query: state.query,
      transform: state.transform,
      activeModelId: state.activeModelId
    }));
  }

  function restoreViewState() {
    const raw = localStorage.getItem(VIEW_STATE_KEY);
    if (!raw) return false;
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed.expanded)) {
        state.expanded = new Set(parsed.expanded);
        state.expanded.add("root");
      }
      state.query = normalizeSearch(parsed.query || "");
      els.searchInput.value = parsed.query || "";
      if (parsed.transform && Number.isFinite(parsed.transform.x) && Number.isFinite(parsed.transform.y) && Number.isFinite(parsed.transform.k)) {
        state.transform = parsed.transform;
      }
      if (parsed.activeModelId) activateModelPath(parsed.activeModelId);
      return true;
    } catch (error) {
      console.warn("Failed to restore model library view state", error);
      return false;
    }
  }

  function walkTree(node, callback) {
    callback(node);
    node.children.forEach((child) => walkTree(child, callback));
  }

  function nodeMatches(node, query) {
    if (!query) return true;
    if (node.type === "model") {
      if (isModelIdQuery(query)) return node.model.id.toLowerCase() === query;
      return node.model.searchText.includes(query);
    }
    return node.children.some((child) => nodeMatches(child, query));
  }

  function visibleChildren(node) {
    if (node.type === "model") return [];
    const children = node.children.slice().sort((a, b) => a.order - b.order);
    if (state.query) return children.filter((child) => nodeMatches(child, state.query));
    if (node.type === "root" || state.expanded.has(node.key)) return children;
    return [];
  }

  function findPathNode(root, predicate) {
    let found = null;
    walkTree(root, (node) => {
      if (found || node.type === "root") return;
      if (predicate(node)) found = node;
    });
    return found;
  }

  function computePathSet(node) {
    const path = new Set();
    let current = node;
    while (current) {
      path.add(current.key);
      current = current.parent;
    }
    return path;
  }

  function clearPendingModelClick() {
    if (state.pendingModelClickTimer) {
      window.clearTimeout(state.pendingModelClickTimer);
      state.pendingModelClickTimer = 0;
    }
  }

  function clearActivePath() {
    state.activePath = new Set();
    state.activeModelId = "";
  }

  function deactivateModelPath() {
    clearActivePath();
    clearPendingModelClick();
    render();
  }

  function activateModelPath(modelId) {
    const modelNode = findPathNode(state.tree, (node) => node.type === "model" && node.model.id === modelId);
    if (!modelNode) return;
    state.activeModelId = modelId;
    state.activePath = computePathSet(modelNode);
    let current = modelNode.parent;
    while (current) {
      state.expanded.add(current.key);
      current = current.parent;
    }
  }

  function isNodeOnActivePath(node) {
    return state.activePath.has(node.key);
  }

  function getNodeCardFromEvent(event) {
    if (event.target && event.target.closest) {
      const closest = event.target.closest(".tree-node");
      if (closest) return closest;
    }
    const path = typeof event.composedPath === "function" ? event.composedPath() : [];
    for (const item of path) {
      if (item && item.dataset && item.dataset.nodeType && item.dataset.nodeKey) return item;
    }
    return null;
  }

  function resolveNodeFromCard(card) {
    if (!card || !state.tree) return null;
    const nodeType = card.dataset.nodeType;
    const nodeKey = card.dataset.nodeKey;
    if (!nodeType || !nodeKey) return null;
    return findPathNode(state.tree, (node) => node.type === nodeType && node.key === nodeKey);
  }

  function getFocusNode() {
    if (!state.tree) return null;
    if (state.activeModelId) {
      return findPathNode(state.tree, (node) => node.type === "model" && node.model.id === state.activeModelId);
    }
    if (isModelIdQuery(state.query)) {
      return findPathNode(state.tree, (node) => node.type === "model" && node.model.id.toLowerCase() === state.query);
    }
    return null;
  }

  function focusOnNode(node) {
    if (!node) return;
    focusOnNodes([node]);
  }

  function focusOnNodes(nodes) {
    if (!nodes || !nodes.length) return;
    const svgRect = els.svg.getBoundingClientRect();
    const smallViewport = svgRect.width < 640;
    const boxes = nodes.map((node) => {
      const size = nodeSize(node);
      return {
        minX: node.x - size.width / 2,
        maxX: node.x + size.width / 2,
        minY: node.y - size.height / 2,
        maxY: node.y + size.height / 2
      };
    });
    const minX = Math.min(...boxes.map((box) => box.minX));
    const maxX = Math.max(...boxes.map((box) => box.maxX));
    const minY = Math.min(...boxes.map((box) => box.minY));
    const maxY = Math.max(...boxes.map((box) => box.maxY));
    const width = Math.max(1, maxX - minX);
    const height = Math.max(1, maxY - minY);
    const padding = nodes.length > 1 ? (smallViewport ? 90 : 128) : (smallViewport ? 64 : 88);
    const fitScale = Math.min(
      (svgRect.width - padding * 2) / width,
      (svgRect.height - padding * 2) / height
    );
    const minScale = smallViewport ? 0.38 : 0.18;
    const maxScale = nodes.length > 1 ? (smallViewport ? 0.92 : 1.08) : (smallViewport ? 0.96 : 1.18);
    const scale = Math.max(minScale, Math.min(maxScale, fitScale));
    state.transform = {
      x: (svgRect.width - width * scale) / 2 - minX * scale,
      y: (svgRect.height - height * scale) / 2 - minY * scale,
      k: scale
    };
    applyTransform();
  }

  function layoutTree(root) {
    let row = 0;
    const nodes = [];
    const links = [];

    function place(node) {
      const children = visibleChildren(node);
      const x = node.depth * H_GAP;
      let y;

      if (children.length) {
        children.forEach((child) => {
          links.push({ source: node, target: child });
          place(child);
        });
        y = children.reduce((sum, child) => sum + child.y, 0) / children.length;
      } else {
        y = row * V_GAP;
        row += 1;
      }

      node.x = x;
      node.y = y;
      nodes.push(node);
    }

    place(root);

    const minY = Math.min(...nodes.map((node) => node.y));
    const maxY = Math.max(...nodes.map((node) => node.y));
    const centerY = (minY + maxY) / 2;
    nodes.forEach((node) => {
      node.y -= centerY;
    });

    return { nodes, links };
  }

  function nodeSize(node) {
    return NODE_SIZE[node.type] || NODE_SIZE.group;
  }

  function render(options = {}) {
    if (!state.tree) return;
    const preserveTransform = Boolean(options.preserveTransform);
    const layout = layoutTree(state.tree);
    state.lastLayout = layout;
    els.viewport.replaceChildren();
    const hasActivePath = state.activePath.size > 0;
    const activeNodes = [];
    const inactiveNodes = [];
    const activeLinks = [];
    const inactiveLinks = [];

    layout.links.forEach((link) => {
      const activeLink = isNodeOnActivePath(link.source) && isNodeOnActivePath(link.target);
      if (activeLink) activeLinks.push(link);
      else inactiveLinks.push(link);
    });
    layout.nodes.forEach((node) => {
      if (hasActivePath && isNodeOnActivePath(node)) activeNodes.push(node);
      else inactiveNodes.push(node);
    });

    const edgeLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
    const nodeLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
    els.viewport.append(edgeLayer, nodeLayer);

    inactiveLinks.concat(activeLinks).forEach((link) => {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const activeLink = isNodeOnActivePath(link.source) && isNodeOnActivePath(link.target);
      const classes = ["tree-link"];
      if (activeLink) classes.push("active-link");
      else if (hasActivePath) classes.push("ghost-link");
      path.setAttribute("class", classes.join(" "));
      path.setAttribute("d", edgePath(link.source, link.target));
      edgeLayer.appendChild(path);
    });

    inactiveNodes.concat(activeNodes).forEach((node) => {
      nodeLayer.appendChild(renderNode(node));
    });

    if (preserveTransform) {
      applyTransform();
    } else if (activeNodes.length) focusOnNodes(activeNodes);
    else {
      const focusNode = getFocusNode();
      if (focusNode) focusOnNodes([focusNode]);
      else fitView();
    }
    updateStats();
    els.emptyState.hidden = !(state.query && !state.tree.children.some((child) => nodeMatches(child, state.query)));
  }

  function edgePath(source, target) {
    const sourceSize = nodeSize(source);
    const targetSize = nodeSize(target);
    const sx = source.x + sourceSize.width / 2;
    const sy = source.y;
    const tx = target.x - targetSize.width / 2;
    const ty = target.y;
    const curve = Math.max(80, (tx - sx) * 0.45);
    return `M ${sx} ${sy} C ${sx + curve} ${sy}, ${tx - curve} ${ty}, ${tx} ${ty}`;
  }

  function renderNode(node) {
    const size = nodeSize(node);
    const hasActivePath = state.activePath.size > 0;
    const onActivePath = isNodeOnActivePath(node);
    const foreignObject = document.createElementNS("http://www.w3.org/2000/svg", "foreignObject");
    foreignObject.setAttribute("x", node.x - size.width / 2);
    foreignObject.setAttribute("y", node.y - size.height / 2);
    foreignObject.setAttribute("width", size.width);
    foreignObject.setAttribute("height", size.height);
    foreignObject.classList.add("node-foreign");

    const card = document.createElement("div");
    const classes = ["tree-node", node.type];
    card.dataset.nodeType = node.type;
    card.dataset.nodeKey = node.key;
    card.dataset.modelId = node.type === "model" ? node.model.id : "";
    if (state.query && nodeMatches(node, state.query)) classes.push("match");
    if (hasActivePath && !onActivePath) classes.push("ghost");
    if (onActivePath) classes.push("active-path");
    if (node.type === "model" && node.model.id === state.activeModelId) classes.push("active-leaf");
    if (node.type !== "model" && node.children.length) {
      classes.push("branch");
      classes.push(visibleChildren(node).length ? "expanded" : "collapsed");
    }
    card.className = classes.join(" ");
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    card.setAttribute("title", node.type === "model" ? node.model.currentName : node.label);
    if (node.type === "model") card.dataset.modelId = node.model.id;

    if (node.type === "model") {
      card.classList.add("leaf-card");

      const id = document.createElement("div");
      id.className = "node-id";
      id.textContent = node.model.id;

      const title = document.createElement("div");
      title.className = "node-title";
      title.textContent = node.model.currentName;

      card.append(id, title);
    } else {
      const title = document.createElement("div");
      title.className = "node-title";
      title.textContent = node.label;
      card.append(title);
    }

    foreignObject.appendChild(card);

    return foreignObject;
  }

  function updateStats() {
    if (!els.modelCount || !els.sceneSummary || !els.sourceBadge) return;
    const sceneCounts = new Map(SCENE_HEADERS.map((scene) => [scene, 0]));
    state.models.forEach((model) => {
      model.scenes.forEach((scene) => {
        if (sceneCounts.has(scene)) sceneCounts.set(scene, sceneCounts.get(scene) + 1);
      });
    });
    els.modelCount.textContent = `${state.models.length} 个模型`;
    els.sceneSummary.textContent = SCENE_HEADERS.map((scene) => `${shortSceneName(scene)} ${sceneCounts.get(scene)}`).join(" / ");
    els.sourceBadge.textContent = state.isCustom ? `自定义数据：${state.sourceName}` : `默认数据：${state.sourceName}`;
    els.sourceBadge.classList.toggle("custom", state.isCustom);
  }

  function shortSceneName(scene) {
    return {
      "车端-低级别自动驾驶（<L3）": "<L3",
      "车端-高级别自动驾驶（≥L3）": "≥L3",
      "车路协同": "车路",
      "端边云协同": "端边云"
    }[scene] || scene;
  }

  function fitView() {
    if (!state.lastLayout || !state.lastLayout.nodes.length) return;
    const svgRect = els.svg.getBoundingClientRect();
    const boxes = state.lastLayout.nodes.map((node) => {
      const size = nodeSize(node);
      return {
        minX: node.x - size.width / 2,
        maxX: node.x + size.width / 2,
        minY: node.y - size.height / 2,
        maxY: node.y + size.height / 2
      };
    });
    const minX = Math.min(...boxes.map((box) => box.minX));
    const maxX = Math.max(...boxes.map((box) => box.maxX));
    const minY = Math.min(...boxes.map((box) => box.minY));
    const maxY = Math.max(...boxes.map((box) => box.maxY));
    const width = Math.max(1, maxX - minX);
    const height = Math.max(1, maxY - minY);
    const isSmallViewport = svgRect.width < 640;
    const minScale = isSmallViewport ? 0.42 : 0.18;
    const fitScale = Math.min((svgRect.width - 80) / width, (svgRect.height - 80) / height);
    const scale = Math.max(minScale, Math.min(1.08, fitScale));
    state.transform = {
      x: isSmallViewport && width * scale > svgRect.width
        ? 24 - minX * scale
        : (svgRect.width - width * scale) / 2 - minX * scale,
      y: (svgRect.height - height * scale) / 2 - minY * scale,
      k: scale
    };
    applyTransform();
  }

  function applyTransform() {
    const { x, y, k } = state.transform;
    els.viewport.setAttribute("transform", `translate(${x}, ${y}) scale(${k})`);
    saveViewState();
  }

  function expandAll() {
    walkTree(state.tree, (node) => {
      if (node.type !== "model") state.expanded.add(node.key);
    });
    render();
    fitView();
    saveViewState();
  }

  function collapseAll() {
    state.expanded.clear();
    state.expanded.add("root");
    render();
    fitView();
    saveViewState();
  }

  function showToast(message) {
    window.clearTimeout(state.toastTimer);
    els.toast.textContent = message;
    els.toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      els.toast.hidden = true;
    }, 3200);
  }

  function parseWorkbook(arrayBuffer, fileName) {
    if (!window.XLSX) throw new Error("Excel 解析库未加载");
    const workbook = window.XLSX.read(arrayBuffer, { type: "array" });
    const sheetName = workbook.SheetNames[0];
    if (!sheetName) throw new Error("Excel 中没有工作表");
    const sheet = workbook.Sheets[sheetName];
    const rows = window.XLSX.utils.sheet_to_json(sheet, { defval: "", raw: false });
    const models = rows.map(normalizeModel).filter(Boolean);
    if (!models.length) throw new Error("没有识别到有效模型记录");
    return {
      version: 1,
      sourceName: fileName,
      importedAt: new Date().toISOString(),
      models
    };
  }

  function applyPayload(payload, persist) {
    state.models = (payload.models || []).map(normalizeModel).filter(Boolean);
    state.sourceName = payload.sourceName || "默认数据";
    state.isCustom = Boolean(payload.isCustom);
    state.tree = createTree(state.models);
    setInitialExpansion(state.tree);
    clearActivePath();
    clearPendingModelClick();
    state.query = "";
    els.searchInput.value = "";
    if (persist) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        version: 1,
        sourceName: payload.sourceName,
        importedAt: payload.importedAt || new Date().toISOString(),
        models: state.models
      }));
      state.isCustom = true;
      localStorage.removeItem(VIEW_STATE_KEY);
    }
    const restored = !persist && restoreViewState();
    render({ preserveTransform: restored });
    if (!restored) fitView();
    saveViewState();
  }

  async function handleExcelFile(file) {
    try {
      const buffer = await file.arrayBuffer();
      const payload = parseWorkbook(buffer, file.name);
      applyPayload({ ...payload, isCustom: true }, true);
      showToast("Excel 导入成功");
    } catch (error) {
      showToast(`导入失败：${error.message}`);
    } finally {
      els.excelInput.value = "";
    }
  }

  function bindEvents() {
    document.addEventListener("click", (event) => {
      const card = getNodeCardFromEvent(event);
      if (!card) return;
      const node = resolveNodeFromCard(card);
      if (!node) return;
      event.preventDefault();
      if (node.type === "model") {
        const now = Date.now();
        const isDoubleClickSequence = state.lastModelClick.id === node.model.id && now - state.lastModelClick.time < 360;
        state.lastModelClick = { id: node.model.id, time: now };
        if (isDoubleClickSequence) {
          clearPendingModelClick();
          return;
        }
        clearPendingModelClick();
        if (state.activeModelId === node.model.id) {
          state.pendingModelClickTimer = window.setTimeout(() => {
            deactivateModelPath();
            state.pendingModelClickTimer = 0;
          }, 260);
          return;
        }
        state.pendingModelClickTimer = window.setTimeout(() => {
          activateModelPath(node.model.id);
          render();
          state.pendingModelClickTimer = 0;
          saveViewState();
        }, 260);
        return;
      }
      clearPendingModelClick();
      clearActivePath();
      if (node.type !== "root") {
        if (state.expanded.has(node.key)) state.expanded.delete(node.key);
        else state.expanded.add(node.key);
      } else {
        state.expanded.add(node.key);
      }
      render();
      saveViewState();
    });

    document.addEventListener("dblclick", (event) => {
      const card = getNodeCardFromEvent(event);
      if (!card) return;
      const node = resolveNodeFromCard(card);
      if (!node || node.type !== "model") return;
      event.preventDefault();
      clearPendingModelClick();
      window.location.href = `model.html?id=${encodeURIComponent(node.model.id)}`;
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const card = getNodeCardFromEvent(event);
      if (!card) return;
      const node = resolveNodeFromCard(card);
      if (!node) return;
      event.preventDefault();
      clearPendingModelClick();
      if (node.type === "model") {
        if (state.activeModelId === node.model.id) {
          deactivateModelPath();
          return;
        }
        activateModelPath(node.model.id);
        render();
        saveViewState();
      } else {
        clearActivePath();
        if (node.type !== "root") {
          if (state.expanded.has(node.key)) state.expanded.delete(node.key);
          else state.expanded.add(node.key);
        } else {
          state.expanded.add(node.key);
        }
        render();
        saveViewState();
      }
    });

    els.importBtn.addEventListener("click", () => els.excelInput.click());
    els.excelInput.addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) handleExcelFile(file);
    });
    els.expandBtn.addEventListener("click", expandAll);
    els.collapseBtn.addEventListener("click", collapseAll);
    els.fitBtn.addEventListener("click", fitView);
    els.resetBtn.addEventListener("click", () => {
      if (!window.confirm("确定恢复默认数据吗？")) return;
      localStorage.removeItem(STORAGE_KEY);
      localStorage.removeItem(VIEW_STATE_KEY);
      const payload = loadPayload();
      applyPayload({ ...payload, sourceName: payload.sourceName, isCustom: payload.isCustom }, false);
      showToast("已恢复默认数据");
    });
    els.searchInput.addEventListener("input", (event) => {
      state.query = normalizeSearch(event.target.value);
      render();
      saveViewState();
    });

    els.svg.addEventListener("pointerdown", (event) => {
      if (event.target.closest && event.target.closest(".node-foreign")) return;
      els.svg.setPointerCapture(event.pointerId);
      els.svg.classList.add("is-panning");
      state.pan = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        baseX: state.transform.x,
        baseY: state.transform.y
      };
    });
    els.svg.addEventListener("pointermove", (event) => {
      if (!state.pan || state.pan.pointerId !== event.pointerId) return;
      state.transform.x = state.pan.baseX + event.clientX - state.pan.startX;
      state.transform.y = state.pan.baseY + event.clientY - state.pan.startY;
      applyTransform();
    });
    els.svg.addEventListener("pointerup", endPan);
    els.svg.addEventListener("pointercancel", endPan);
    els.svg.addEventListener("wheel", (event) => {
      event.preventDefault();
      const rect = els.svg.getBoundingClientRect();
      const mouseX = event.clientX - rect.left;
      const mouseY = event.clientY - rect.top;
      const worldX = (mouseX - state.transform.x) / state.transform.k;
      const worldY = (mouseY - state.transform.y) / state.transform.k;
      const factor = event.deltaY < 0 ? 1.08 : 0.92;
      const nextScale = Math.max(0.16, Math.min(2.4, state.transform.k * factor));
      state.transform.x = mouseX - worldX * nextScale;
      state.transform.y = mouseY - worldY * nextScale;
      state.transform.k = nextScale;
      applyTransform();
    }, { passive: false });
    window.addEventListener("resize", () => {
      window.requestAnimationFrame(fitView);
    });
  }

  function endPan(event) {
    if (!state.pan || state.pan.pointerId !== event.pointerId) return;
    state.pan = null;
    els.svg.classList.remove("is-panning");
  }

  function init() {
    bindEvents();
    const payload = loadPayload();
    applyPayload(payload, false);
  }

  init();
})();
