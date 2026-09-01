(function () {
  "use strict";

  const STORAGE_KEY = "collab_model_library_v1";
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

  const els = {
    title: document.getElementById("detailTitle"),
    subtitle: document.getElementById("detailSubtitle"),
    notFound: document.getElementById("notFound"),
    detailContent: document.getElementById("detailContent"),
    modelId: document.getElementById("modelId"),
    currentName: document.getElementById("currentName"),
    codeLink: document.getElementById("codeLink"),
    sceneTags: document.getElementById("sceneTags"),
    pathTags: document.getElementById("pathTags"),
    description: document.getElementById("description")
  };

  function clean(value) {
    return value == null ? "" : String(value).replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
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
      codeUrl
    };
  }

  function loadModels() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        const models = (parsed.models || []).map(normalizeModel).filter(Boolean);
        if (models.length) {
          return {
            models,
            source: `自定义数据：${clean(parsed.sourceName) || "已导入 Excel"}`
          };
        }
      } catch (error) {
        console.warn("Failed to read imported model data", error);
      }
    }

    const defaults = window.DEFAULT_MODEL_LIBRARY || { sourceFile: "默认数据", models: [] };
    return {
      models: (defaults.models || []).map(normalizeModel).filter(Boolean),
      source: `默认数据：${defaults.sourceFile || "内置数据"}`
    };
  }

  function addTag(container, text, className) {
    const tag = document.createElement("span");
    tag.className = className;
    tag.textContent = text;
    container.appendChild(tag);
  }

  function buildPathFlow(steps) {
    const flow = document.createElement("div");
    flow.className = "path-flow";

    steps.forEach((stepData, index) => {
      const step = document.createElement("div");
      const classes = ["path-step"];
      if (stepData.kind) classes.push(stepData.kind);
      step.className = classes.join(" ");

      const label = document.createElement("div");
      label.className = "path-step-label";
      label.textContent = stepData.label;

      const meta = document.createElement("div");
      meta.className = "path-step-meta";
      meta.append(label);

      step.append(meta);
      flow.appendChild(step);

      if (index < steps.length - 1) {
        const connector = document.createElement("div");
        connector.className = "path-step-connector";
        connector.setAttribute("aria-hidden", "true");
        flow.appendChild(connector);
      }
    });

    return flow;
  }

  function render(model) {
    document.title = `${model.id} - 模型详情`;
    els.title.textContent = model.currentName || "模型详情";
    els.subtitle.textContent = `模型编号：${model.id}`;
    els.modelId.textContent = model.id;
    els.currentName.textContent = model.currentName || "待补充";
    els.description.textContent = model.description || "待补充";

    els.sceneTags.replaceChildren();
    if (model.scenes.length) {
      model.scenes.forEach((scene) => addTag(els.sceneTags, scene, `scene-tag ${SCENE_CLASS[scene] || ""}`));
    } else {
      addTag(els.sceneTags, "待补充", "path-tag");
    }

    els.pathTags.replaceChildren();
    if (model.path.length) {
      const steps = [
        { label: ROOT_LABEL, kind: "root-step" },
        ...model.path.map((pathItem) => ({
          label: pathItem,
          kind: "level-step"
        })),
        { label: `${model.id} · ${model.currentName}`, kind: "terminal-step" }
      ];
      els.pathTags.appendChild(buildPathFlow(steps));
    } else {
      els.pathTags.appendChild(buildPathFlow([
        { label: ROOT_LABEL, kind: "root-step" },
        { label: `${model.id} · ${model.currentName}`, kind: "terminal-step" }
      ]));
    }

    if (model.codeUrl && model.codeUrl !== "无") {
      els.codeLink.href = model.codeUrl;
      els.codeLink.textContent = model.codeUrl;
      els.codeLink.classList.remove("disabled");
      els.codeLink.removeEventListener("click", preventPlaceholderLink);
    } else {
      els.codeLink.href = "#";
      els.codeLink.textContent = "无";
      els.codeLink.classList.add("disabled");
      els.codeLink.addEventListener("click", preventPlaceholderLink);
    }

    els.detailContent.hidden = false;
    els.notFound.hidden = true;
  }

  function preventPlaceholderLink(event) {
    event.preventDefault();
  }

  function renderNotFound(id) {
    document.title = "未找到模型";
    els.title.textContent = "未找到模型";
    els.subtitle.textContent = id ? `模型编号：${id}` : "";
    els.detailContent.hidden = true;
    els.notFound.hidden = false;
  }

  function init() {
    const id = clean(new URLSearchParams(window.location.search).get("id"));
    const payload = loadModels();
    const model = payload.models.find((item) => item.id === id);
    if (model) render(model);
    else renderNotFound(id);
  }

  init();
})();
