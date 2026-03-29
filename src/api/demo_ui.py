"""
自主式交通系统端端协同感知 — 演示可视化 UI

GET /demo 返回本页面，通过 WebSocket /demo/ws 接收实时事件，驱动：
  - 车辆俯视行驶 Canvas 动画（道路 + 感知扇形 + 通信波束）
  - 工作流执行步骤面板
  - 实时事件日志
"""


def get_demo_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>端端协同感知演示 — LangManus</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#0f1117;color:#e2e8f0;min-height:100vh;overflow-x:hidden}

/* ── Header ───────────────────────────────────────────────── */
header{
  background:linear-gradient(135deg,#1a1a3e 0%,#0f1117 100%);
  border-bottom:1px solid #2d3748;
  padding:14px 28px;display:flex;align-items:center;justify-content:space-between;
}
header h1{font-size:18px;font-weight:700;letter-spacing:.5px;color:#e2e8f0}
header h1 span{color:#63b3ed}
.hbadge{background:#2b6cb0;color:#bee3f8;border-radius:4px;
        font-size:11px;padding:3px 9px;font-weight:600;margin-left:10px}
.hbadge.live{background:#276749;color:#9ae6b4;animation:pulse-badge 2s infinite}
@keyframes pulse-badge{0%,100%{opacity:1}50%{opacity:.6}}
.hstatus{font-size:13px;color:#718096;display:flex;align-items:center;gap:8px}
.status-dot{width:8px;height:8px;border-radius:50%;background:#718096}
.status-dot.connected{background:#48bb78;box-shadow:0 0 6px #48bb78}

/* ── Layout ───────────────────────────────────────────────── */
.layout{display:grid;grid-template-columns:480px 1fr;grid-template-rows:auto 1fr;
        gap:16px;padding:16px;max-width:1400px;margin:0 auto;height:calc(100vh - 65px)}
.panel{background:#1a202c;border:1px solid #2d3748;border-radius:12px;overflow:hidden}
.panel-title{padding:12px 18px;font-size:13px;font-weight:600;color:#a0aec0;
             text-transform:uppercase;letter-spacing:.8px;
             border-bottom:1px solid #2d3748;display:flex;align-items:center;gap:8px}
.panel-title .dot{width:7px;height:7px;border-radius:50%;background:#4a5568}
.panel-title .dot.active{background:#48bb78;animation:pulse-dot 1.5s infinite}
@keyframes pulse-dot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.8)}}

/* ── Canvas Road ─────────────────────────────────────────── */
.road-wrap{padding:16px;display:flex;flex-direction:column;gap:12px}
#road-canvas{width:100%;border-radius:8px;display:block;background:#111827}

/* ── Vehicle Status Bar ──────────────────────────────────── */
.vstat-row{display:flex;gap:8px;flex-wrap:wrap}
.vstat-item{display:flex;align-items:center;gap:5px;font-size:11px;color:#718096;
            background:#111827;border:1px solid #2d3748;border-radius:6px;
            padding:4px 10px}
.vstat-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.vstat-name{color:#a0aec0}
.vstat-label{border-radius:3px;padding:1px 7px;font-size:10px;font-weight:600;
             margin-left:2px}
.vstat-label.idle{background:#2d3748;color:#718096}
.vstat-label.active{background:#2b6cb0;color:#bee3f8}
.vstat-label.done{background:#276749;color:#9ae6b4}
.vstat-label.failed{background:#c53030;color:#fff}
.vstat-label.failover{background:#c05621;color:#fbd38d}

/* ── Controls ─────────────────────────────────────────────── */
.ctrl-row{display:flex;gap:10px;flex-wrap:wrap}
.btn{padding:9px 20px;border:none;border-radius:8px;cursor:pointer;
     font-size:13px;font-weight:600;transition:all .15s;display:inline-flex;
     align-items:center;gap:6px;letter-spacing:.3px}
.btn-start{background:linear-gradient(135deg,#2b6cb0,#2c5282);color:#bee3f8;
           box-shadow:0 2px 8px rgba(43,108,176,.4)}
.btn-start:hover:not(:disabled){background:linear-gradient(135deg,#3182ce,#2b6cb0);
   transform:translateY(-1px);box-shadow:0 4px 12px rgba(49,130,206,.5)}
.btn-start:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-toggle{background:#2d3748;color:#e2e8f0;border:1px solid #4a5568}
.btn-toggle:hover{background:#4a5568}
.btn-toggle.active{background:linear-gradient(135deg,#c05621,#9c4221);color:#fbd38d;
                   border-color:#c05621;box-shadow:0 2px 8px rgba(192,86,33,.4)}
.btn-reset{background:#2d3748;color:#a0aec0;border:1px solid #4a5568}
.btn-reset:hover{background:#4a5568}

/* ── Right-side panels ─────────────────────────────────────── */
.right-col{display:flex;flex-direction:column;gap:16px;overflow:hidden}

/* Steps panel */
.steps-wrap{padding:16px 18px;flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.step-item{border:1px solid #2d3748;border-radius:8px;padding:12px 14px;
           transition:all .3s;background:#111827}
.step-item.pending{border-color:#2d3748;opacity:.65}
.step-item.running{border-color:#4299e1;background:#1a2744;
                   box-shadow:0 0 0 1px #4299e1;animation:step-glow 1.5s infinite}
.step-item.done{border-color:#48bb78;background:#1a2c1e}
.step-item.failed{border-color:#fc8181;background:#2c1a1a}
.step-item.failover{border-color:#f6ad55;background:#2c2014}
@keyframes step-glow{0%,100%{box-shadow:0 0 0 1px #4299e1}
                     50%{box-shadow:0 0 0 2px #4299e1,0 0 8px rgba(66,153,225,.4)}}
.step-header{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.step-icon{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;
           justify-content:center;font-size:12px;flex-shrink:0}
.step-icon.pending{background:#2d3748;color:#718096}
.step-icon.running{background:#2b6cb0;color:#fff;animation:spin 1.2s linear infinite}
.step-icon.done{background:#276749;color:#fff}
.step-icon.failed{background:#c53030;color:#fff}
.step-icon.failover{background:#c05621;color:#fff}
@keyframes spin{to{transform:rotate(360deg)}}
.step-title{font-size:13px;font-weight:600;color:#e2e8f0}
.step-agent{font-size:11px;color:#718096;margin-top:2px}
.step-result{font-size:11px;color:#a0aec0;margin-top:8px;padding:8px 10px;
             background:#0f1117;border-radius:6px;max-height:90px;overflow-y:auto;
             line-height:1.6;white-space:pre-wrap;word-break:break-all}
.empty-steps{color:#4a5568;font-size:13px;text-align:center;padding:40px 0}

/* Log panel */
.log-wrap{padding:0}
#log-box{height:160px;overflow-y:auto;padding:10px 14px;
         font-family:"SF Mono","Fira Code",monospace;font-size:11px;
         line-height:1.7;color:#68d391;background:#0a0f1a}
.log-line{padding:1px 0}
.log-line.error{color:#fc8181}
.log-line.warn{color:#f6ad55}
.log-line.info{color:#68d391}
.log-line.event{color:#63b3ed}
.log-line.system{color:#a0aec0}

/* Result area */
.result-wrap{padding:16px 18px}
#result-box{background:#0f1117;border:1px solid #2d3748;border-radius:8px;
            padding:14px;min-height:80px;font-size:13px;line-height:1.7;
            color:#a0aec0;white-space:pre-wrap;max-height:200px;overflow-y:auto}
#result-box.has-result{color:#e2e8f0;border-color:#48bb78}

/* Demo info strip */
.demo-info{background:#1a2744;border:1px solid #2b4c8c;border-radius:8px;
           padding:12px 16px;font-size:12px;color:#90cdf4;line-height:1.6}
.demo-info strong{color:#bee3f8}

/* Scrollbars */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2d3748;border-radius:3px}
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center">
    <h1>🚗 自主交通 <span>端端协同感知</span> 演示</h1>
    <span class="hbadge" id="hbadge">DEMO</span>
  </div>
  <div class="hstatus">
    <div class="status-dot" id="ws-dot"></div>
    <span id="ws-label">WebSocket 未连接</span>
  </div>
</header>

<div class="layout">

  <!-- ── 左列：拓扑 + 控制 ── -->
  <div style="display:flex;flex-direction:column;gap:16px;overflow:hidden">

    <!-- 车辆行驶 Canvas -->
    <div class="panel" style="flex:1">
      <div class="panel-title">
        <div class="dot" id="topo-dot"></div>
        车辆行驶 / 协同感知场景
      </div>
      <div class="road-wrap">
        <canvas id="road-canvas" width="440" height="300"></canvas>

        <!-- 节点状态栏 -->
        <div class="vstat-row">
          <div class="vstat-item">
            <span class="vstat-dot" style="background:#48bb78"></span>
            <span class="vstat-name">自车 A</span>
            <span class="vstat-label idle" id="vstat-self">待机</span>
          </div>
          <div class="vstat-item">
            <span class="vstat-dot" style="background:#4299e1"></span>
            <span class="vstat-name">协同 B</span>
            <span class="vstat-label idle" id="vstat-vehicleB">待机</span>
          </div>
          <div class="vstat-item">
            <span class="vstat-dot" style="background:#f6ad55"></span>
            <span class="vstat-name">备援 C</span>
            <span class="vstat-label idle" id="vstat-vehicleC">备用</span>
          </div>
          <div class="vstat-item">
            <span class="vstat-dot" style="background:#63b3ed"></span>
            <span class="vstat-name">认知中枢</span>
            <span class="vstat-label idle" id="vstat-cognition">待机</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 控制面板 -->
    <div class="panel">
      <div class="panel-title"><div class="dot"></div>演示控制</div>
      <div style="padding:16px 18px;display:flex;flex-direction:column;gap:12px">
        <div class="demo-info">
          <strong>场景：</strong>自车向协同车辆请求协同认知，编排层动态调度感知 Agent，
          融合多源数据生成综合环境认知报告。<br>
          开启 <strong>VehicleB 故障</strong> 可观察 §2.3 故障转移至备援节点 VehicleC。
        </div>
        <div class="ctrl-row">
          <button class="btn btn-start" id="btn-start" onclick="startDemo()">
            ▶ 启动协同感知演示
          </button>
          <button class="btn btn-toggle" id="btn-fail" onclick="toggleFail()">
            ⚡ VehicleB 故障
          </button>
          <button class="btn btn-reset" onclick="resetUI()">↺ 重置</button>
        </div>
        <div id="task-input-row" style="display:flex;gap:8px">
          <input id="task-input" type="text"
            value="自主感知前方200m路段，并请求VehicleB协同补充盲区数据，输出综合环境认知报告"
            style="flex:1;background:#0f1117;border:1px solid #2d3748;border-radius:7px;
                   padding:8px 12px;font-size:12px;color:#e2e8f0;outline:none"/>
        </div>
      </div>
    </div>
  </div>

  <!-- ── 右列：步骤 + 日志 ── -->
  <div class="right-col">

    <!-- 工作流步骤 -->
    <div class="panel" style="flex:1;display:flex;flex-direction:column;overflow:hidden">
      <div class="panel-title"><div class="dot" id="steps-dot"></div>工作流执行步骤</div>
      <div class="steps-wrap" id="steps-wrap">
        <div class="empty-steps">等待工作流启动…</div>
      </div>
    </div>

    <!-- 执行结果 -->
    <div class="panel">
      <div class="panel-title"><div class="dot" id="res-dot"></div>综合认知报告</div>
      <div class="result-wrap">
        <div id="result-box">等待工作流完成…</div>
      </div>
    </div>

    <!-- 事件日志 -->
    <div class="panel">
      <div class="panel-title"><div class="dot" id="log-dot"></div>实时事件日志</div>
      <div id="log-box"></div>
    </div>

  </div>
</div>

<script>
// ── State ───────────────────────────────────────────────────
let ws = null;
let vehicleBFailed = false;
let demoRunning = false;
let steps = [];

// ── Canvas: Constants ───────────────────────────────────────
const CW = 440, CH = 300;
const ROAD_TOP = 38, ROAD_BOT = 270;
const ROAD_H = ROAD_BOT - ROAD_TOP;          // 232
const LANE_H = ROAD_H / 3;                   // ~77.3
const LANE_Y = [
  ROAD_TOP + LANE_H * 0.5,                   // ~76.7  Lane A
  ROAD_TOP + LANE_H * 1.5,                   // ~154   Lane B
  ROAD_TOP + LANE_H * 2.5,                   // ~231.3 Lane C
];
const CAR_W = 42, CAR_H = 22;                 // car body dimensions

// ── Canvas: Vehicle models ───────────────────────────────────
const vehicles = {
  self: {
    x: 70,  laneY: LANE_Y[0], speed: 1.2, baseSpeed: 1.2,
    color: '#48bb78', dark: '#276749', label: 'A',
    state: 'idle', scanActive: false, beamToB: false, beamToC: false,
    opacity: 1.0, scanScale: 1.0, scanDir: 1,
  },
  vehicleB: {
    x: 220, laneY: LANE_Y[1], speed: 1.0, baseSpeed: 1.0,
    color: '#4299e1', dark: '#2b6cb0', label: 'B',
    state: 'idle', scanActive: false, beamToB: false, beamToC: false,
    opacity: 1.0, scanScale: 1.0, scanDir: 1,
  },
  vehicleC: {
    x: 360, laneY: LANE_Y[2], speed: 0.7, baseSpeed: 0.7,
    color: '#f6ad55', dark: '#c05621', label: 'C',
    state: 'idle', scanActive: false, beamToB: false, beamToC: false,
    opacity: 0.4, scanScale: 1.0, scanDir: 1,
  },
};

let roadOffset = 0;
let beamDash = 0;

// ── Canvas: Drawing helpers ──────────────────────────────────
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
}

function drawRoad(ctx) {
  // Background (grass/sky)
  ctx.fillStyle = '#111827';
  ctx.fillRect(0, 0, CW, CH);
  ctx.fillStyle = '#162216';
  ctx.fillRect(0, 0, CW, ROAD_TOP);
  ctx.fillRect(0, ROAD_BOT, CW, CH - ROAD_BOT);

  // Road surface
  ctx.fillStyle = '#1e2535';
  ctx.fillRect(0, ROAD_TOP, CW, ROAD_H);

  // Shoulder tint
  ctx.fillStyle = 'rgba(44,53,71,0.4)';
  ctx.fillRect(0, ROAD_TOP, CW, 5);
  ctx.fillRect(0, ROAD_BOT - 5, CW, 5);

  // Road edge lines
  ctx.strokeStyle = '#c8d4e8';
  ctx.lineWidth = 2.5;
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.moveTo(0, ROAD_TOP); ctx.lineTo(CW, ROAD_TOP); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, ROAD_BOT); ctx.lineTo(CW, ROAD_BOT); ctx.stroke();

  // Lane dividers (animated dashes, moving right → gives impression road flows past)
  ctx.strokeStyle = 'rgba(200,212,232,0.32)';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([24, 16]);
  ctx.lineDashOffset = -roadOffset;
  [1, 2].forEach(i => {
    const y = ROAD_TOP + LANE_H * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(CW, y); ctx.stroke();
  });
  ctx.setLineDash([]);
  ctx.lineDashOffset = 0;
}

function drawVehicle(ctx, v) {
  if (v.opacity <= 0.02) return;
  ctx.save();
  ctx.globalAlpha = v.opacity;
  ctx.translate(v.x, v.laneY);

  const W = CAR_W, H = CAR_H;

  // Drop shadow
  ctx.fillStyle = 'rgba(0,0,0,0.4)';
  roundRect(ctx, -W/2 + 3, -H/2 + 5, W, H, 5);
  ctx.fill();

  // Body fill
  let bodyColor = v.color;
  if (v.state === 'failed') {
    bodyColor = (Math.floor(Date.now() / 260) % 2 === 0) ? '#fc8181' : '#6b1f1f';
  }
  ctx.fillStyle = bodyColor;
  roundRect(ctx, -W/2, -H/2, W, H, 5);
  ctx.fill();

  // Body stroke
  ctx.strokeStyle =
    v.state === 'failed' ? '#fc8181' :
    v.state === 'active' ? v.color :
    v.dark;
  ctx.lineWidth = v.state === 'active' ? 2.5 : 1.5;
  roundRect(ctx, -W/2, -H/2, W, H, 5);
  ctx.stroke();

  // Hood (front = right side, car moves right)
  ctx.fillStyle = v.state === 'failed' ? '#6b1f1f' : v.dark;
  roundRect(ctx, W/2 - 11, -H/2 + 2, 9, H - 4, 4);
  ctx.fill();

  // Windshield
  ctx.fillStyle = 'rgba(186,230,253,0.55)';
  ctx.fillRect(W/2 - 22, -H/2 + 4, 9, H - 8);

  // Roof label letter
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 11px -apple-system,sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(v.label, -5, 1);

  // Active glow ring
  if (v.state === 'active') {
    ctx.strokeStyle = v.color;
    ctx.lineWidth = 2;
    ctx.globalAlpha = v.opacity * 0.35;
    roundRect(ctx, -W/2 - 5, -H/2 - 5, W + 10, H + 10, 9);
    ctx.stroke();
  }

  ctx.restore();
}

function drawScanFan(ctx, v) {
  if (!v.scanActive || v.opacity < 0.15) return;
  ctx.save();
  ctx.globalAlpha = v.opacity * 0.52;

  // Fan origin = front edge of car
  ctx.translate(v.x + CAR_W / 2, v.laneY);

  // Breathe in / out
  v.scanScale += v.scanDir * 0.013;
  if (v.scanScale > 1.18) v.scanDir = -1;
  if (v.scanScale < 0.80) v.scanDir = 1;

  const len = 88 * v.scanScale;
  const span = Math.PI / 5.5;     // ~32.7° each side → 65° total fan

  // Filled sector with radial gradient
  const grad = ctx.createRadialGradient(0, 0, 4, 0, 0, len);
  grad.addColorStop(0,   v.color + 'cc');
  grad.addColorStop(0.55, v.color + '66');
  grad.addColorStop(1,   v.color + '00');
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.arc(0, 0, len, -span, span);
  ctx.closePath();
  ctx.fill();

  // Sector outline
  ctx.strokeStyle = v.color + '99';
  ctx.lineWidth = 1;
  ctx.globalAlpha = v.opacity * 0.28;
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.arc(0, 0, len, -span, span);
  ctx.closePath();
  ctx.stroke();

  ctx.restore();
}

function drawBeam(ctx, ax, ay, bx, by) {
  ctx.save();
  ctx.globalAlpha = 0.72;
  ctx.strokeStyle = '#4299e1';
  ctx.lineWidth = 2;
  ctx.setLineDash([10, 7]);
  ctx.lineDashOffset = beamDash;
  ctx.beginPath();
  ctx.moveTo(ax, ay);
  ctx.lineTo(bx, by);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();
}

// ── Canvas: Main loop ────────────────────────────────────────
function gameLoop() {
  const canvas = document.getElementById('road-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  // Advance simulation
  roadOffset = (roadOffset + 1.5) % 40;
  beamDash -= 0.9;

  Object.values(vehicles).forEach(v => {
    if (v.speed > 0) {
      v.x += v.speed;
      if (v.x - CAR_W / 2 > CW + 8) v.x = -CAR_W / 2 - 8;
    }
  });

  // Draw layers
  drawRoad(ctx);

  // Scan fans (below vehicles)
  Object.values(vehicles).forEach(v => drawScanFan(ctx, v));

  // Communication beams between vehicles
  const sv = vehicles.self, bv = vehicles.vehicleB, cv = vehicles.vehicleC;
  if (sv.beamToB) drawBeam(ctx, sv.x, sv.laneY, bv.x, bv.laneY);
  if (sv.beamToC) drawBeam(ctx, sv.x, sv.laneY, cv.x, cv.laneY);

  // Vehicles on top
  Object.values(vehicles).forEach(v => drawVehicle(ctx, v));

  requestAnimationFrame(gameLoop);
}

// ── Canvas: State helpers ────────────────────────────────────
function setNodeStatus(vehicle, state, label) {
  const v = vehicles[vehicle];  // undefined for 'cognition' — that's fine
  if (v) {
    v.state = state;
    if (state === 'active') v.scanActive = true;
    if (state === 'done'  || state === 'idle')  v.scanActive = false;
    if (state === 'failed') { v.scanActive = false; v.speed = 0; }
  }
  const el = document.getElementById('vstat-' + vehicle);
  if (el && label) {
    el.textContent = label;
    el.className = 'vstat-label ' + state;
  }
  const anyActive = Object.values(vehicles).some(vv => vv.state === 'active');
  document.getElementById('topo-dot').classList.toggle('active', anyActive);
}

const VEHICLE_BEAM_MAP = { vehicleB: 'beamToB', vehicleC: 'beamToC' };

function activateEdge(vehicle, on) {
  const prop = VEHICLE_BEAM_MAP[vehicle];
  if (prop) vehicles.self[prop] = on;
}

// ── WebSocket ───────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/demo/ws`);

  ws.onopen = () => {
    setWsStatus(true);
    addLog('system', '✓ WebSocket 已连接到事件总线');
  };

  ws.onclose = () => {
    setWsStatus(false);
    addLog('warn', '! WebSocket 连接断开，5s 后重连…');
    setTimeout(connectWS, 5000);
  };

  ws.onerror = () => {
    addLog('error', '✗ WebSocket 连接错误');
  };

  ws.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handleEvent(event.type, event.payload || {});
    } catch(err) {
      addLog('error', '解析事件失败: ' + err.message);
    }
  };
}

// ── Event Handler ───────────────────────────────────────────
function handleEvent(type, payload) {
  addLog('event', `[${type}] ${JSON.stringify(payload).substring(0, 120)}`);

  if (type === 'log') {
    addLog(payload.level || 'info', payload.message || '');
    return;
  }

  if (type === 'demo:plan_ready') {
    renderSteps(payload.tasks || []);
    addLog('info', `📋 规划完成，共 ${(payload.tasks||[]).length} 个任务`);
    setNodeStatus('cognition', 'active', '规划中');
    return;
  }

  if (type === 'demo:dispatch_start') {
    const v = payload.vehicle;
    setNodeStatus(v, 'active', '执行中');
    activateEdge(v, true);
    const idx = steps.findIndex(s => agentMatchesVehicle(s.agent, v) && s.status === 'pending');
    if (idx >= 0) { steps[idx].status = 'running'; renderSteps(); }
    addLog('info', `🚀 分发给 ${v}：${payload.task || ''}`);
    return;
  }

  if (type === 'demo:dispatch_done') {
    const v = payload.vehicle;
    const ok = payload.success !== false;
    setNodeStatus(v, ok ? 'done' : 'failed', ok ? '完成' : '失败');
    activateEdge(v, false);
    const idx = steps.findIndex(s => agentMatchesVehicle(s.agent, v) && s.status === 'running');
    if (idx >= 0) {
      steps[idx].status = ok ? 'done' : 'failed';
      steps[idx].result = payload.result || '';
      renderSteps();
    }
    return;
  }

  if (type === 'demo:failover') {
    const from_ = payload.from || 'vehicleB';
    const to_   = payload.to   || 'vehicleC';

    // Stop failed vehicle in place
    const vFrom = vehicles[from_];
    if (vFrom) { vFrom.state = 'failed'; vFrom.scanActive = false; vFrom.speed = 0; }
    setNodeStatus(from_, 'failed', '⚡故障');
    activateEdge(from_, false);

    // Activate backup vehicle
    const vTo = vehicles[to_];
    if (vTo) { vTo.opacity = 1.0; vTo.speed = vTo.baseSpeed; vTo.state = 'active'; vTo.scanActive = true; }
    setNodeStatus(to_, 'active', '接管');
    activateEdge(to_, true);

    const idx = steps.findIndex(s => agentMatchesVehicle(s.agent, from_) && s.status === 'running');
    if (idx >= 0) { steps[idx].status = 'failover'; steps[idx].result = payload.reason || ''; renderSteps(); }
    addLog('warn', `⚡ Failover: ${from_} → ${to_}  ${payload.reason || ''}`);
    return;
  }

  if (type === 'demo:status') {
    vehicleBFailed = payload.vehicleB_failed === true;
    updateFailBtn();
    return;
  }

  if (type === 'demo:task_complete') {
    demoRunning = false;
    updateStartBtn();
    document.getElementById('btn-fail').disabled = false;
    setNodeStatus('cognition', 'done', '完成');
    const rb = document.getElementById('result-box');
    rb.textContent = payload.result || '（无结果）';
    rb.classList.add('has-result');
    document.getElementById('res-dot').classList.add('active');
    addLog('info', '✅ 工作流执行完毕');
    return;
  }
}

// ── Steps render ────────────────────────────────────────────
const ICON_MAP = {pending:'⬤', running:'↻', done:'✓', failed:'✗', failover:'⚡'};
const AGENT_LABEL = {
  perception_self_001:    '自车感知 · VehicleA',
  perception_vehicleB_001:'协同感知 · VehicleB',
  perception_vehicleC_001:'备援感知 · VehicleC',
  cognition_main_001:     '认知融合 · 中枢',
};

function renderSteps(newTasks) {
  if (newTasks) {
    steps = newTasks.map(t => ({id: t.id, title: t.title, agent: t.agent,
                                status: 'pending', result: ''}));
  }
  const wrap = document.getElementById('steps-wrap');
  if (!steps.length) {
    wrap.innerHTML = '<div class="empty-steps">等待工作流启动…</div>';
    return;
  }
  wrap.innerHTML = steps.map((s, i) => `
    <div class="step-item ${s.status}">
      <div class="step-header">
        <div class="step-icon ${s.status}">${ICON_MAP[s.status] || '⬤'}</div>
        <div>
          <div class="step-title">${i+1}. ${s.title}</div>
          <div class="step-agent">${AGENT_LABEL[s.agent] || s.agent}</div>
        </div>
      </div>
      ${s.result ? `<div class="step-result">${escHtml(s.result)}</div>` : ''}
    </div>
  `).join('');
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function agentMatchesVehicle(agentId, vehicle) {
  const map = {
    self:      'perception_self_001',
    vehicleB:  'perception_vehicleB_001',
    vehicleC:  'perception_vehicleC_001',
    cognition: 'cognition_main_001',
  };
  return agentId === map[vehicle];
}

// ── Log ─────────────────────────────────────────────────────
function addLog(level, msg) {
  const box = document.getElementById('log-box');
  const time = new Date().toLocaleTimeString('zh-CN', {hour12:false});
  const line = document.createElement('div');
  line.className = `log-line ${level}`;
  line.textContent = `[${time}] ${msg}`;
  box.appendChild(line);
  if (box.children.length > 200) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

// ── Controls ─────────────────────────────────────────────────
function setWsStatus(ok) {
  document.getElementById('ws-dot').className = 'status-dot' + (ok ? ' connected' : '');
  document.getElementById('ws-label').textContent = ok ? 'WebSocket 已连接' : 'WebSocket 未连接';
  const badge = document.getElementById('hbadge');
  badge.classList.toggle('live', ok);
  badge.textContent = ok ? 'LIVE' : 'DEMO';
}

function _resetCanvas() {
  Object.values(vehicles).forEach(v => {
    v.state = 'idle';
    v.scanActive = false;
    v.speed = v.baseSpeed;
    v.opacity = 1.0;
    v.scanScale = 1.0;
    v.scanDir = 1;
  });
  vehicles.self.beamToB = false;
  vehicles.self.beamToC = false;
  vehicles.vehicleC.opacity = 0.4;
  ['self','vehicleB','vehicleC','cognition'].forEach(id => {
    const el = document.getElementById('vstat-' + id);
    if (el) {
      el.textContent = id === 'vehicleC' ? '备用' : '待机';
      el.className = 'vstat-label idle';
    }
  });
  document.getElementById('topo-dot').classList.remove('active');
}

function startDemo() {
  if (demoRunning) return;
  const task = document.getElementById('task-input').value.trim()
    || '自主感知前方200m路段，请求VehicleB协同补充盲区数据，输出综合环境认知报告';

  steps = [];
  renderSteps();
  document.getElementById('result-box').textContent = '等待工作流完成…';
  document.getElementById('result-box').classList.remove('has-result');
  document.getElementById('res-dot').classList.remove('active');
  _resetCanvas();

  demoRunning = true;
  updateStartBtn();
  document.getElementById('btn-fail').disabled = true;
  setNodeStatus('cognition', 'active', '编排中');
  addLog('info', `▶ 启动演示工作流：${task}`);

  fetch('/demo/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({task_description: task})
  }).then(r => r.json()).then(d => {
    addLog('info', `工作流 ID: ${d.workflow_id}`);
  }).catch(err => {
    addLog('error', '启动失败: ' + err.message);
    demoRunning = false;
    updateStartBtn();
    document.getElementById('btn-fail').disabled = false;
  });
}

function toggleFail() {
  fetch('/demo/toggle-vehicleB-failure', {method:'POST'})
    .then(r => r.json())
    .then(d => {
      vehicleBFailed = d.vehicleB_failed;
      updateFailBtn();
      addLog(vehicleBFailed ? 'warn' : 'info',
        vehicleBFailed ? '⚡ VehicleB 故障模拟已开启' : '✓ VehicleB 恢复正常');
    })
    .catch(err => addLog('error', '切换失败: ' + err.message));
}

function updateFailBtn() {
  const btn = document.getElementById('btn-fail');
  btn.classList.toggle('active', vehicleBFailed);
  btn.textContent = vehicleBFailed ? '⚡ VehicleB 故障 [ON]' : '⚡ VehicleB 故障';
}

function updateStartBtn() {
  const btn = document.getElementById('btn-start');
  btn.disabled = demoRunning;
  btn.textContent = demoRunning ? '⏳ 工作流执行中…' : '▶ 启动协同感知演示';
}

function resetUI() {
  if (demoRunning) return;
  steps = [];
  renderSteps();
  document.getElementById('result-box').textContent = '等待工作流完成…';
  document.getElementById('result-box').classList.remove('has-result');
  document.getElementById('res-dot').classList.remove('active');
  _resetCanvas();
  document.getElementById('log-box').innerHTML = '';
  addLog('system', '↺ UI 已重置');
}

// ── Init ─────────────────────────────────────────────────────
connectWS();
gameLoop();
addLog('system', '自主交通端端协同感知演示已加载，等待工作流启动...');
</script>
</body>
</html>"""
