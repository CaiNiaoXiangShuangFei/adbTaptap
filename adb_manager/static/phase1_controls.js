(function () {
  "use strict";

  let syncEnabled = false;
  let wallOpen = false;
  let wallTimer = null;
  const wallUrls = new Map();

  const style = document.createElement("style");
  style.textContent = `
    .control-modal-card{width:min(1180px,96vw);max-height:92vh;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 24px 80px rgba(0,0,0,.55)}
    .control-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}.control-head h2{margin-right:auto}
    .wall-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}.wall-device{background:var(--bg);border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:pointer}.wall-device img{display:block;width:100%;height:360px;object-fit:contain;background:#020617}.wall-device div{padding:8px;font-size:12px;color:var(--muted);word-break:break-all}
    .advanced-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:8px 0}.advanced-row input,.advanced-row select{min-width:180px;flex:1;background:var(--card2);border:1px solid var(--line);color:var(--text);padding:8px;border-radius:8px}
    .sync-active{border-color:var(--green)!important;color:var(--green)!important}.task-device-controls{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.task-device-controls .btn{padding:5px 8px;font-size:11px}
    .pv-advanced{width:min(920px,96vw);margin:8px auto}.quick-grid{display:flex;gap:6px;flex-wrap:wrap}
  `;
  document.head.appendChild(style);

  const wallModal = document.createElement("div");
  wallModal.className = "modal";
  wallModal.id = "deviceWallModal";
  wallModal.innerHTML = `
    <div class="control-modal-card">
      <div class="control-head"><h2>多设备监控墙</h2><span class="muted" id="wallStatus"></span><button class="btn" id="wallRefresh">立即刷新</button><button class="btn danger" id="wallClose">关闭</button></div>
      <div class="wall-grid" id="wallGrid"></div>
    </div>`;
  document.body.appendChild(wallModal);

  const controlModal = document.createElement("div");
  controlModal.className = "modal";
  controlModal.id = "quickControlModal";
  controlModal.innerHTML = `
    <div class="control-modal-card" style="width:min(760px,96vw)">
      <div class="control-head"><h2>设备快捷控制中心</h2><button class="btn danger" id="quickClose">关闭</button></div>
      <div class="advanced-row"><label>目标设备</label><select id="quickDevice"></select><button class="btn" id="quickUseSelected">使用已勾选设备</button></div>
      <div class="advanced-row"><input id="quickText" placeholder="输入要发送到手机的文字（中文建议用本地 scrcpy 粘贴）"><button class="btn primary" id="quickSendText">发送文字</button><button class="btn" id="quickPasteText">粘贴电脑剪贴板</button><button class="btn" id="quickReadClipboard">读取手机剪贴板</button></div>
      <div class="quick-grid" id="quickActions"></div>
      <div class="advanced-row"><label>亮度</label><input id="quickBrightness" type="range" min="1" max="255" value="128"><button class="btn" id="quickSetBrightness">设置亮度</button></div>
      <div class="settings-hint">scrcpy 窗口默认支持电脑键盘和双向剪贴板；网页控制用于批量发送和快捷操作。</div>
    </div>`;
  document.body.appendChild(controlModal);

  const actions = [
    ["主页", "home"], ["返回", "back"], ["最近任务", "recent"], ["唤醒", "wake"], ["息屏", "sleep"],
    ["音量+", "volume_up"], ["音量-", "volume_down"], ["静音", "volume_mute"],
    ["通知栏", "notifications"], ["快捷设置", "quick_settings"], ["收起面板", "collapse_panels"],
    ["自动旋转", "rotate_auto"], ["竖屏", "rotate_portrait"], ["横屏", "rotate_landscape"],
  ];
  document.getElementById("quickActions").innerHTML = actions.map(([label, action]) =>
    `<button class="btn" data-quick-action="${action}">${label}</button>`
  ).join("");

  const header = document.querySelector(".head-actions");
  const wallButton = document.createElement("button");
  wallButton.className = "btn"; wallButton.textContent = "多设备监控墙";
  const controlButton = document.createElement("button");
  controlButton.className = "btn"; controlButton.textContent = "快捷控制";
  header.insertBefore(controlButton, document.getElementById("btnSettings"));
  header.insertBefore(wallButton, controlButton);

  function onlineDevices() { return latestDevices.filter(device => device.state === "device"); }
  function selectedTargets(fallbackSerial) {
    const selected = [...selectedDevices].filter(serial => onlineDevices().some(device => device.serial === serial));
    return selected.length ? selected : (fallbackSerial ? [fallbackSerial] : []);
  }
  function quickTargets() {
    const selected = document.getElementById("quickDevice").value;
    return document.getElementById("quickUseSelected").classList.contains("sync-active") ? selectedTargets(selected) : (selected ? [selected] : []);
  }
  function refreshQuickDevices() {
    const select = document.getElementById("quickDevice");
    const previous = select.value;
    select.innerHTML = onlineDevices().map(device => `<option value="${esc(device.serial)}">${esc(device.model || device.serial)} · ${esc(device.serial)}</option>`).join("");
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
  }
  async function postDevice(serial, endpoint, body) {
    return apiPost(`/api/devices/${encodeURIComponent(serial)}/${endpoint}`, body);
  }
  async function sendMany(endpoint, body, fallbackSerial) {
    const targets = fallbackSerial ? (syncEnabled ? selectedTargets(fallbackSerial) : [fallbackSerial]) : quickTargets();
    if (!targets.length) { toast("请选择在线设备", "err"); return []; }
    return Promise.all(targets.map(serial => postDevice(serial, endpoint, body)));
  }

  async function refreshWall() {
    if (!wallOpen) return;
    const devices = onlineDevices();
    const grid = document.getElementById("wallGrid");
    grid.innerHTML = devices.map(device => `<div class="wall-device" data-wall-device="${esc(device.serial)}"><img data-wall-image="${esc(device.serial)}"><div>${esc(device.model || "Android")}<br>${esc(device.serial)}</div></div>`).join("");
    document.getElementById("wallStatus").textContent = devices.length + " 台在线设备 · 点击画面打开本地预览";
    await Promise.all(devices.map(async device => {
      try {
        const blob = await fetchScreenshot(device.serial);
        if (!wallOpen) return;
        const image = grid.querySelector(`[data-wall-image="${CSS.escape(device.serial)}"]`);
        if (!image) return;
        const previous = wallUrls.get(device.serial);
        const url = URL.createObjectURL(blob);
        wallUrls.set(device.serial, url); image.src = url;
        if (previous) URL.revokeObjectURL(previous);
      } catch (_) {}
    }));
    wallTimer = setTimeout(refreshWall, 900);
  }
  function openWall() { wallOpen = true; wallModal.classList.add("show"); refreshWall(); }
  function closeWall() { wallOpen = false; clearTimeout(wallTimer); wallModal.classList.remove("show"); wallUrls.forEach(url => URL.revokeObjectURL(url)); wallUrls.clear(); }
  wallButton.addEventListener("click", openWall);
  document.getElementById("wallClose").addEventListener("click", closeWall);
  document.getElementById("wallRefresh").addEventListener("click", () => { clearTimeout(wallTimer); refreshWall(); });
  document.getElementById("wallGrid").addEventListener("click", event => { const card = event.target.closest("[data-wall-device]"); if (card) openNativePreview(card.dataset.wallDevice); });

  controlButton.addEventListener("click", () => { refreshQuickDevices(); controlModal.classList.add("show"); });
  document.getElementById("quickClose").addEventListener("click", () => controlModal.classList.remove("show"));
  document.getElementById("quickUseSelected").addEventListener("click", event => { event.currentTarget.classList.toggle("sync-active"); event.currentTarget.textContent = event.currentTarget.classList.contains("sync-active") ? "正在批量控制已勾选设备" : "使用已勾选设备"; });
  document.getElementById("quickSendText").addEventListener("click", () => sendMany("text", {text: document.getElementById("quickText").value}));
  document.getElementById("quickPasteText").addEventListener("click", async () => { try { document.getElementById("quickText").value = await navigator.clipboard.readText(); } catch (_) { toast("无法读取电脑剪贴板", "err"); } });
  document.getElementById("quickReadClipboard").addEventListener("click", async () => { const serial = document.getElementById("quickDevice").value; const data = await postDevice(serial, "clipboard", {operation:"get"}); if (data && data.ok) { await navigator.clipboard.writeText(data.text || ""); toast("手机剪贴板已复制到电脑", "ok"); } });
  document.getElementById("quickActions").addEventListener("click", event => { const button = event.target.closest("[data-quick-action]"); if (button) sendMany("quick", {action:button.dataset.quickAction}); });
  document.getElementById("quickSetBrightness").addEventListener("click", () => sendMany("quick", {action:"brightness", value:document.getElementById("quickBrightness").value}));

  const advanced = document.createElement("div");
  advanced.className = "pv-advanced";
  advanced.innerHTML = `<div class="advanced-row"><input id="pvTextInput" placeholder="向当前设备或所有已勾选设备输入文字"><button class="btn" id="pvPaste">粘贴</button><button class="btn primary" id="pvSendText">发送</button><button class="btn" id="pvSyncToggle">同步操控：关闭</button></div><div class="settings-hint" id="pvSyncHint">开启后，当前预览的点击、滑动和按键会按分辨率比例同步到已勾选设备。</div>`;
  document.getElementById("pvModal").insertBefore(advanced, document.querySelector("#pvModal .pv-bar"));
  document.getElementById("pvSyncToggle").addEventListener("click", event => { syncEnabled = !syncEnabled; event.currentTarget.classList.toggle("sync-active", syncEnabled); event.currentTarget.textContent = "同步操控：" + (syncEnabled ? "开启" : "关闭"); });
  document.getElementById("pvPaste").addEventListener("click", async () => { try { document.getElementById("pvTextInput").value = await navigator.clipboard.readText(); } catch (_) { toast("无法读取电脑剪贴板", "err"); } });
  document.getElementById("pvSendText").addEventListener("click", () => sendMany("text", {text:document.getElementById("pvTextInput").value}, streamSerial));

  const originalPreviewSend = previewSend;
  previewSend = function (url, body) {
    originalPreviewSend(url, body);
    if (!syncEnabled || !streamSerial) return;
    const action = url.split("/").pop();
    const master = onlineDevices().find(device => device.serial === streamSerial);
    const masterMatch = String(master && master.resolution || "").match(/(\d+)\D+(\d+)/);
    selectedTargets(streamSerial).filter(serial => serial !== streamSerial).forEach(serial => {
      let mapped = Object.assign({}, body);
      const target = onlineDevices().find(device => device.serial === serial);
      const targetMatch = String(target && target.resolution || "").match(/(\d+)\D+(\d+)/);
      if (masterMatch && targetMatch && (action === "tap" || action === "swipe")) {
        const sx = Number(targetMatch[1]) / Number(masterMatch[1]);
        const sy = Number(targetMatch[2]) / Number(masterMatch[2]);
        ["x","x1","x2"].forEach(key => { if (key in mapped) mapped[key] = Math.round(mapped[key] * sx); });
        ["y","y1","y2"].forEach(key => { if (key in mapped) mapped[key] = Math.round(mapped[key] * sy); });
      }
      postDevice(serial, action, mapped);
    });
  };

  function renderTaskControls() {
    let container = document.getElementById("taskDeviceControls");
    if (!container) { container = document.createElement("div"); container.id = "taskDeviceControls"; container.className = "task-device-controls"; document.getElementById("taskStatus").after(container); }
    const task = taskStates.find(item => item.serial === activeLogSerial);
    if (!task || !["running","pause_pending","paused"].includes(task.status)) { container.innerHTML = ""; return; }
    const paused = ["pause_pending","paused"].includes(task.status);
    container.innerHTML = `<button class="btn" data-task-action="${paused ? "resume" : "pause"}">${paused ? "继续此设备" : "暂停此设备"}</button><button class="btn" data-task-action="skip">跳过当前</button><button class="btn" data-task-action="retry">重试当前</button>`;
  }
  document.addEventListener("click", async event => { const button = event.target.closest("[data-task-action]"); if (!button || !activeLogSerial) return; button.disabled = true; await apiPost("/api/task/control", {serial:activeLogSerial, action:button.dataset.taskAction}); pollTasks(); });
  setInterval(renderTaskControls, 600);
})();
