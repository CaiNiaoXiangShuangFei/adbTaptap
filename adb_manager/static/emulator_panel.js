(function () {
  "use strict";

  let emulatorPollTimer = null;
  let installLogSize = 0;
  let editingProfile = "";
  let latestProfiles = [];

  const style = document.createElement("style");
  style.textContent = `
    .emu-drawer{position:absolute;z-index:8;top:0;right:0;width:min(410px,92vw);height:100%;display:flex;flex-direction:column;overflow:hidden;background:rgba(8,15,30,.94);backdrop-filter:blur(22px);border-left:1px solid rgba(148,163,184,.25);box-shadow:-18px 0 50px rgba(0,0,0,.38);transition:transform .22s ease;transform:translateX(0)}
    .emu-drawer.collapsed{transform:translateX(calc(100% - 44px))}.emu-drawer.collapsed .emu-drawer-head,.emu-drawer.collapsed .emu-drawer-body{visibility:hidden}.emu-handle{position:absolute;z-index:20;left:0;top:50%;transform:translateY(-50%);width:44px;height:80px;border:0;border-right:1px solid var(--line);border-radius:0 12px 12px 0;background:var(--green);color:#03120a;font-size:24px;font-weight:800;cursor:pointer}
    .emu-drawer-head{padding:16px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}.emu-drawer-head h3{margin:0;flex:1}.emu-drawer-body{padding:14px;overflow:auto;flex:1}.emu-section{border:1px solid var(--line);background:rgba(15,23,42,.5);border-radius:13px;padding:12px;margin-bottom:12px}.emu-section h4{margin:0 0 10px}.emu-form{display:grid;grid-template-columns:1fr 1fr;gap:8px}.emu-form input{min-width:0;width:100%;box-sizing:border-box;background:var(--card2);border:1px solid var(--line);color:var(--text);padding:8px;border-radius:8px}.emu-form .full{grid-column:1/-1}.emu-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.emu-profile{padding:10px;border:1px solid var(--line);border-radius:10px;margin-top:8px;background:rgba(2,6,23,.45)}.emu-profile-title{display:flex;align-items:center;gap:7px}.emu-profile-title b{flex:1;word-break:break-all}.emu-progress{height:7px;border-radius:10px;background:#0f172a;overflow:hidden;margin:8px 0}.emu-progress span{display:block;height:100%;background:linear-gradient(90deg,#22c55e,#38bdf8)}.emu-install-log{max-height:150px;overflow:auto;white-space:pre-wrap;font:11px/1.45 Consolas,monospace;color:#94a3b8;background:#020617;padding:8px;border-radius:8px}.emu-safe-note{font-size:11px;color:#94a3b8;line-height:1.5}.emu-status-dot{width:8px;height:8px;border-radius:50%;background:#64748b;display:inline-block}.emu-status-dot.on{background:#22c55e;box-shadow:0 0 8px #22c55e}
    @media(max-width:720px){.emu-drawer{width:calc(100vw - 42px)}.emu-form{grid-template-columns:1fr}}
  `;
  document.head.appendChild(style);

  const drawer = document.createElement("aside");
  drawer.id = "emulatorDrawer";
  drawer.className = "emu-drawer collapsed";
  drawer.innerHTML = `
    <button class="emu-handle" id="emuDrawerHandle" title="展开/收起虚拟安卓手机">‹</button>
    <div class="emu-drawer-head"><h3>内置虚拟安卓手机</h3><button class="btn" id="emuRefresh">刷新</button><button class="btn" id="emuCollapse">收起</button></div>
    <div class="emu-drawer-body">
      <div class="emu-section">
        <h4>运行环境</h4>
        <div id="emuRuntimeStatus" class="muted">正在检查…</div>
        <div class="emu-progress"><span id="emuInstallProgress" style="width:0%"></span></div>
        <div class="emu-actions"><button class="btn primary" id="emuInstall">安装/补全运行时</button></div>
        <pre class="emu-install-log" id="emuInstallLog">所有 SDK、JDK、系统镜像、AVD 和临时文件均保存在项目 runtime 目录。</pre>
      </div>
      <div class="emu-section">
        <h4>预览设置</h4>
        <label style="display:flex;align-items:center;gap:8px"><input type="checkbox" id="emuAlwaysTop">新打开的原生预览始终置顶</label>
        <div class="emu-safe-note">置顶通过 scrcpy 的 --always-on-top 实现；网页预览仍在当前管理页面中。</div>
      </div>
      <div class="emu-section">
        <h4>创建全新测试手机</h4>
        <div class="emu-form">
          <input class="full" id="emuName" placeholder="名称，如 TapTap_Test_01">
          <input id="emuBrand" value="Google" placeholder="品牌测试标签">
          <input id="emuModel" value="Pixel Test Device" placeholder="型号测试标签">
          <input id="emuWidth" type="number" value="1080" min="480" max="2160" placeholder="宽度">
          <input id="emuHeight" type="number" value="2400" min="800" max="3840" placeholder="高度">
          <input id="emuDpi" type="number" value="420" min="160" max="640" placeholder="DPI">
          <input id="emuMemory" type="number" value="3072" min="1536" max="8192" placeholder="内存 MB">
        </div>
        <div class="emu-actions"><button class="btn primary" id="emuCreate">创建虚拟手机</button><button class="btn" id="emuEditCancel" style="display:none">取消编辑</button></div>
        <div class="emu-safe-note">品牌和型号是 AVD 测试配置标签。为避免身份规避风险，不提供 IMEI 或系统设备指纹伪造。</div>
      </div>
      <div class="emu-section"><h4>虚拟手机列表</h4><div id="emuProfiles"><span class="muted">暂无虚拟手机</span></div></div>
    </div>`;
  document.getElementById("pvModal").appendChild(drawer);

  const headerButton = document.createElement("button");
  headerButton.className = "btn";
  headerButton.textContent = "虚拟安卓手机";
  document.querySelector(".head-actions").insertBefore(headerButton, document.getElementById("btnSettings"));

  function setDrawer(open) {
    drawer.classList.toggle("collapsed", !open);
    document.getElementById("emuDrawerHandle").textContent = open ? "›" : "‹";
    if (open) refreshEmulatorPanel();
    else clearTimeout(emulatorPollTimer);
  }
  function showDrawer() {
    document.getElementById("pvModal").classList.add("show");
    setDrawer(true);
  }
  headerButton.onclick = showDrawer;
  document.getElementById("emuDrawerHandle").onclick = () => setDrawer(drawer.classList.contains("collapsed"));
  document.getElementById("emuCollapse").onclick = () => setDrawer(false);
  document.getElementById("emuRefresh").onclick = refreshEmulatorPanel;

  const alwaysTop = document.getElementById("emuAlwaysTop");
  alwaysTop.checked = localStorage.getItem("previewAlwaysOnTop") === "1";
  alwaysTop.onchange = () => localStorage.setItem("previewAlwaysOnTop", alwaysTop.checked ? "1" : "0");

  async function fetchJson(url) {
    try {
      const response = await fetch(url, {cache:"no-store"});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "请求失败");
      return data;
    } catch (error) {
      toast(error.message, "err");
      return null;
    }
  }

  async function refreshEmulatorPanel() {
    clearTimeout(emulatorPollTimer);
    if (drawer.classList.contains("collapsed")) return;
    const [state, list] = await Promise.all([
      fetchJson("/api/emulator/status"),
      fetchJson("/api/emulator/profiles"),
    ]);
    if (state) renderInstallState(state.install);
    if (list) renderProfiles(list);
    if (state?.install?.status === "running" || list?.profiles?.some(item => item.running)) {
      emulatorPollTimer = setTimeout(refreshEmulatorPanel, 1200);
    }
  }

  function renderInstallState(install) {
    const ready = install.emulator_ready && install.adb_ready && install.jdk_ready && install.system_image_ready;
    document.getElementById("emuRuntimeStatus").innerHTML = `<span class="emu-status-dot ${ready ? "on" : ""}"></span> ${ready ? "运行时已就绪" : esc(install.message || "运行时未安装")}<br><span class="muted">${esc(install.runtime_dir || "")}</span>`;
    document.getElementById("emuInstallProgress").style.width = Math.max(0, Math.min(100, install.progress || (ready ? 100 : 0))) + "%";
    const lines = install.lines || [];
    if (lines.length || !installLogSize) {
      const output = document.getElementById("emuInstallLog");
      output.textContent = lines.join("\n") || "所有 SDK、JDK、系统镜像、AVD 和临时文件均保存在项目 runtime 目录。";
      output.scrollTop = output.scrollHeight;
      installLogSize = lines.length;
    }
    document.getElementById("emuInstall").disabled = install.status === "running";
  }

  function renderProfiles(data) {
    latestProfiles = data.profiles || [];
    const root = document.getElementById("emuProfiles");
    if (!data.profiles.length) {
      root.innerHTML = '<span class="muted">暂无虚拟手机，安装运行时后创建。</span>';
      return;
    }
    root.innerHTML = data.profiles.map(profile => `
      <div class="emu-profile">
        <div class="emu-profile-title"><span class="emu-status-dot ${profile.running ? "on" : ""}"></span><b>${esc(profile.name)}</b><span class="badge">${profile.running ? "运行中" : "已停止"}</span></div>
        <div class="muted">${esc(profile.brand_label)} · ${esc(profile.model_label)}<br>${profile.width}×${profile.height} · ${profile.dpi} DPI · ${profile.memory} MB${profile.serial ? " · " + esc(profile.serial) : ""}</div>
        <div class="emu-actions">
          ${profile.running ? `<button class="btn primary" data-emu-preview="${esc(profile.serial)}">网页预览</button><button class="btn" data-emu-native="${esc(profile.serial)}">原生预览</button><button class="btn" data-emu-action="stop" data-emu-name="${esc(profile.name)}">停止</button><button class="btn" data-emu-action="restart" data-emu-name="${esc(profile.name)}">重启</button>` : `<button class="btn primary" data-emu-action="start" data-emu-name="${esc(profile.name)}">启动</button>`}
          ${profile.running ? "" : `<button class="btn" data-emu-edit="${esc(profile.name)}">编辑配置</button>`}
          <button class="btn" data-emu-action="wipe" data-emu-name="${esc(profile.name)}">恢复全新</button>
          <button class="btn danger" data-emu-action="delete" data-emu-name="${esc(profile.name)}">删除</button>
        </div>
      </div>`).join("");
  }

  document.getElementById("emuInstall").onclick = async () => {
    const data = await apiPost("/api/emulator/install", {});
    if (data?.ok) refreshEmulatorPanel();
  };
  document.getElementById("emuCreate").onclick = async () => {
    const payload = {
      name: document.getElementById("emuName").value,
      brand_label: document.getElementById("emuBrand").value,
      model_label: document.getElementById("emuModel").value,
      width: document.getElementById("emuWidth").value,
      height: document.getElementById("emuHeight").value,
      dpi: document.getElementById("emuDpi").value,
      memory: document.getElementById("emuMemory").value,
    };
    if (editingProfile) payload.name = editingProfile;
    const data = await apiPost(editingProfile ? "/api/emulator/update" : "/api/emulator/create", payload);
    if (data?.ok) {
      resetProfileForm();
      refreshEmulatorPanel();
    }
  };
  function resetProfileForm() {
    editingProfile = "";
    document.getElementById("emuName").disabled = false;
    document.getElementById("emuName").value = "";
    document.getElementById("emuCreate").textContent = "创建虚拟手机";
    document.getElementById("emuEditCancel").style.display = "none";
  }
  document.getElementById("emuEditCancel").onclick = resetProfileForm;
  document.getElementById("emuProfiles").onclick = async event => {
    const preview = event.target.closest("[data-emu-preview]");
    if (preview) {
      openPreview(preview.dataset.emuPreview);
      setDrawer(true);
      return;
    }
    const native = event.target.closest("[data-emu-native]");
    if (native) {
      openNativePreview(native.dataset.emuNative, native);
      return;
    }
    const edit = event.target.closest("[data-emu-edit]");
    if (edit) {
      const profile = latestProfiles.find(item => item.name === edit.dataset.emuEdit);
      if (!profile) return;
      editingProfile = profile.name;
      document.getElementById("emuName").value = profile.name;
      document.getElementById("emuName").disabled = true;
      document.getElementById("emuBrand").value = profile.brand_label;
      document.getElementById("emuModel").value = profile.model_label;
      document.getElementById("emuWidth").value = profile.width;
      document.getElementById("emuHeight").value = profile.height;
      document.getElementById("emuDpi").value = profile.dpi;
      document.getElementById("emuMemory").value = profile.memory;
      document.getElementById("emuCreate").textContent = "保存配置";
      document.getElementById("emuEditCancel").style.display = "";
      return;
    }
    const action = event.target.closest("[data-emu-action]");
    if (!action) return;
    if (action.dataset.emuAction === "wipe" && !confirm("恢复全新会清除该虚拟手机内的全部应用和数据，确定继续吗？")) return;
    if (action.dataset.emuAction === "delete" && !confirm("确定永久删除这个虚拟手机及其 D 盘数据吗？")) return;
    action.disabled = true;
    await apiPost("/api/emulator/action", {name:action.dataset.emuName,action:action.dataset.emuAction});
    setTimeout(refreshEmulatorPanel, 900);
  };

  const originalClosePreview = closePreview;
  closePreview = function (hide) {
    if (hide !== false) setDrawer(false);
    return originalClosePreview(hide);
  };
})();
