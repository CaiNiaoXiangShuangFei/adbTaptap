(function () {
  "use strict";

  let toolboxSerial = "";
  let toolboxTab = "apps";
  let filePath = "/sdcard";
  let logcatOffset = 0;
  let logcatLines = [];
  let logcatTimer = null;
  let recording = false;
  let recordedActions = [];
  let lastRecordedAt = 0;
  let inspectorElements = [];
  let inspectorSerial = "";
  const pluginOffsets = {};

  const style = document.createElement("style");
  style.textContent = `
    .toolkit-card{width:min(1180px,96vw);height:min(820px,92vh);display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:0 28px 90px rgba(0,0,0,.58);overflow:hidden}
    .toolkit-head{display:flex;gap:10px;align-items:center;padding:16px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}.toolkit-head h2{margin-right:auto}.toolkit-tabs{display:flex;gap:6px;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}.toolkit-tabs .active{border-color:var(--green);color:var(--green)}
    .toolkit-body{padding:16px;overflow:auto;flex:1}.tool-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}.tool-row input,.tool-row select{background:var(--card2);border:1px solid var(--line);color:var(--text);padding:9px;border-radius:9px;min-width:160px}.tool-row .grow{flex:1}
    .package-list,.file-list,.record-list,.plugin-list{display:grid;gap:8px}.package-item,.file-item,.record-item,.plugin-item{display:flex;align-items:center;gap:8px;padding:10px 12px;background:rgba(15,23,42,.48);border:1px solid var(--line);border-radius:11px}.package-name,.file-name,.record-name,.plugin-name{min-width:0;flex:1;word-break:break-all}.drop-zone{border:1px dashed var(--green);padding:22px;text-align:center;border-radius:12px;color:var(--muted);margin:10px 0}.drop-zone.drag{background:rgba(34,197,94,.12)}
    .logcat-output,.plugin-output{height:520px;overflow:auto;background:#020617;color:#cbd5e1;border:1px solid var(--line);border-radius:10px;padding:12px;white-space:pre-wrap;font:12px/1.55 Consolas,monospace}
    .inspector-layout{display:grid;grid-template-columns:minmax(320px,2fr) minmax(280px,1fr);gap:12px;min-height:0}.inspector-stage{position:relative;display:inline-block;max-width:100%;align-self:start}.inspector-stage img{display:block;max-width:100%;max-height:680px}.element-box{position:absolute;border:1px solid rgba(56,189,248,.7);background:rgba(56,189,248,.06);padding:0;cursor:pointer}.element-box.clickable{border-color:#22c55e;background:rgba(34,197,94,.08)}.element-box:hover,.element-box.selected{border-width:2px;background:rgba(250,204,21,.18);border-color:#facc15}.element-list{height:680px;overflow:auto}.element-item{padding:8px;border-bottom:1px solid var(--line);font-size:12px;cursor:pointer;word-break:break-all}.element-item:hover{background:var(--card2)}
    .recording-live{border-color:#ef4444!important;color:#ef4444!important;animation:recordPulse 1s infinite}@keyframes recordPulse{50%{opacity:.55}}
    @media(max-width:800px){.inspector-layout{grid-template-columns:1fr}.element-list{height:300px}.toolkit-card{height:94vh}}
  `;
  document.head.appendChild(style);

  function modal(id, html) {
    const root = document.createElement("div"); root.className = "modal"; root.id = id; root.innerHTML = html; document.body.appendChild(root); return root;
  }
  const toolbox = modal("toolboxModal", `<div class="toolkit-card"><div class="toolkit-head"><h2 id="toolboxTitle">设备工具箱</h2><button class="btn" id="toolVirtual">独立虚拟屏幕</button><button class="btn danger" id="toolboxClose">关闭</button></div><div class="toolkit-tabs"><button class="btn active" data-tool-tab="apps">APK 与应用</button><button class="btn" data-tool-tab="files">文件传输</button><button class="btn" data-tool-tab="logcat">Logcat</button><button class="btn" data-tool-tab="debug">调试工具</button></div><div class="toolkit-body" id="toolboxBody"></div></div>`);
  const inspector = modal("inspectorModal", `<div class="toolkit-card"><div class="toolkit-head"><h2>可视化元素检查器</h2><span class="muted" id="inspectorInfo"></span><button class="btn" id="inspectorRefresh">刷新页面元素</button><button class="btn danger" id="inspectorClose">关闭</button></div><div class="toolkit-body inspector-layout"><div style="overflow:auto;text-align:center"><div class="inspector-stage" id="inspectorStage"><img id="inspectorImage"></div></div><div><div class="tool-row"><input class="grow" id="inspectorFilter" placeholder="筛选文本、ID、类名"><button class="btn primary" id="inspectorTap" disabled>点击所选元素</button></div><div class="settings-hint" id="inspectorSelected">点击框或右侧元素查看详情</div><div class="element-list" id="inspectorList"></div></div></div></div>`);
  const recordsModal = modal("recordsModal", `<div class="toolkit-card" style="width:min(900px,96vw)"><div class="toolkit-head"><h2>操作录制与回放</h2><button class="btn" id="refreshRecords">刷新</button><button class="btn" id="stopReplays">停止全部回放</button><button class="btn danger" id="recordsClose">关闭</button></div><div class="toolkit-body"><div class="settings-hint">在网页预览里开始录制，所有点击、滑动、按键和文字输入会保存；回放时会同步运行到当前已勾选设备。</div><div class="record-list" id="recordList"></div><h3>回放状态</h3><div id="replayStates" class="muted">暂无回放任务</div></div></div>`);
  const pluginsModal = modal("pluginsModal", `<div class="toolkit-card" style="width:min(940px,96vw)"><div class="toolkit-head"><h2>脚本插件模组</h2><button class="btn primary" id="runPlugins">在已勾选设备运行</button><button class="btn" id="stopPlugins">停止插件任务</button><button class="btn" id="refreshPlugins">刷新插件</button><button class="btn danger" id="pluginsClose">关闭</button></div><div class="toolkit-body"><div class="settings-hint">将插件目录放入 adb_manager/plugins；每个插件包含 plugin.json 和入口 Python 脚本。勾选即启用，运行时每台设备使用独立线程和日志。</div><div class="plugin-list" id="pluginList"></div><h3>插件运行日志</h3><pre class="plugin-output" id="pluginOutput">暂无插件任务</pre></div></div>`);
  [toolbox, inspector, recordsModal, pluginsModal].forEach(root => root.addEventListener("click", event => {
    if (event.target !== root) return;
    root.classList.remove("show");
    if (root === toolbox) clearTimeout(logcatTimer);
  }));
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const shown = [inspector, toolbox, recordsModal, pluginsModal].find(root => root.classList.contains("show"));
    if (!shown) return;
    event.preventDefault(); event.stopImmediatePropagation(); shown.classList.remove("show");
    if (shown === toolbox) clearTimeout(logcatTimer);
  }, true);

  const head = document.querySelector(".head-actions");
  const recordsButton = document.createElement("button"); recordsButton.className = "btn"; recordsButton.textContent = "录制与回放";
  const pluginsButton = document.createElement("button"); pluginsButton.className = "btn"; pluginsButton.textContent = "插件模组";
  head.insertBefore(pluginsButton, document.getElementById("btnSettings")); head.insertBefore(recordsButton, pluginsButton);

  async function getJson(url) {
    try { const response = await fetch(url, {cache:"no-store"}); const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || "请求失败"); return data; }
    catch (error) { toast(error.message, "err"); return null; }
  }
  async function rawUpload(url, file) {
    try { const response = await fetch(url, {method:"POST",headers:{"Content-Type":"application/octet-stream"},body:file}); const data = await response.json(); toast(data.message, data.ok ? "ok" : "err"); return data; }
    catch (error) { toast("上传失败：" + error.message, "err"); return null; }
  }
  function onlineTargets() { const selected = [...selectedDevices].filter(serial => latestDevices.some(item => item.serial === serial && item.state === "device")); return selected.length ? selected : (toolboxSerial ? [toolboxSerial] : []); }

  function enhanceCards() {
    document.querySelectorAll(".card").forEach(card => {
      if (card.querySelector("[data-open-toolbox]")) return;
      const serialNode = card.querySelector(".serial"); const actions = card.querySelector(".actions");
      if (!serialNode || !actions || !latestDevices.some(item => item.serial === serialNode.textContent.trim() && item.state === "device")) return;
      const button = document.createElement("button"); button.className = "btn primary"; button.dataset.openToolbox = serialNode.textContent.trim(); button.textContent = "设备工具箱"; actions.prepend(button);
    });
  }
  new MutationObserver(enhanceCards).observe(document.getElementById("deviceGrid"), {childList:true,subtree:true}); enhanceCards();
  document.addEventListener("click", event => { const button = event.target.closest("[data-open-toolbox]"); if (button) openToolbox(button.dataset.openToolbox); });

  function openToolbox(serial) { toolboxSerial = serial; document.getElementById("toolboxTitle").textContent = "设备工具箱 · " + serial; toolbox.classList.add("show"); showToolTab(toolboxTab); }
  function closeToolbox() { toolbox.classList.remove("show"); clearTimeout(logcatTimer); }
  document.getElementById("toolboxClose").onclick = closeToolbox;
  document.getElementById("toolVirtual").onclick = () => apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/virtual-display`, {});
  document.querySelector(".toolkit-tabs").addEventListener("click", event => { const button = event.target.closest("[data-tool-tab]"); if (!button) return; toolboxTab = button.dataset.toolTab; document.querySelectorAll("[data-tool-tab]").forEach(item => item.classList.toggle("active", item === button)); showToolTab(toolboxTab); });

  function showToolTab(tab) {
    clearTimeout(logcatTimer);
    if (tab === "apps") renderApps();
    else if (tab === "files") renderFiles(filePath);
    else if (tab === "logcat") renderLogcat();
    else renderDebug();
  }
  async function renderApps() {
    const body = document.getElementById("toolboxBody"); body.innerHTML = `<div class="tool-row"><select id="appScope"><option value="third_party">第三方应用</option><option value="all">全部应用</option></select><button class="btn" id="reloadApps">刷新列表</button><label class="btn primary">安装 APK<input hidden type="file" id="apkFile" accept=".apk,application/vnd.android.package-archive"></label></div><div class="package-list" id="packageList"><span class="muted">正在读取应用列表…</span></div>`;
    document.getElementById("reloadApps").onclick = loadApps; document.getElementById("appScope").onchange = loadApps;
    document.getElementById("apkFile").onchange = async event => { const file = event.target.files[0]; if (!file) return; await rawUpload(`/api/devices/${encodeURIComponent(toolboxSerial)}/install-apk?filename=${encodeURIComponent(file.name)}`, file); loadApps(); };
    loadApps();
  }
  async function loadApps() {
    const scope = document.getElementById("appScope")?.value || "third_party"; const data = await getJson(`/api/devices/${encodeURIComponent(toolboxSerial)}/apps?scope=${scope}`); if (!data) return;
    document.getElementById("packageList").innerHTML = data.packages.length ? data.packages.map(pkg => `<div class="package-item"><span class="package-name">${esc(pkg)}</span><button class="btn" data-app="launch" data-package="${esc(pkg)}">打开</button><button class="btn" data-app="stop" data-package="${esc(pkg)}">停止</button><button class="btn" data-app="clear" data-package="${esc(pkg)}">清数据</button><button class="btn danger" data-app="uninstall" data-package="${esc(pkg)}">卸载</button></div>`).join("") : '<span class="muted">没有应用</span>';
  }
  document.getElementById("toolboxBody").addEventListener("click", async event => { const app = event.target.closest("[data-app]"); if (app) { if (["clear","uninstall"].includes(app.dataset.app) && !confirm(`确定对 ${app.dataset.package} 执行${app.dataset.app === "clear" ? "清除数据" : "卸载"}吗？`)) return; const data = await apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/app`, {package:app.dataset.package,action:app.dataset.app}); if (data?.ok && app.dataset.app === "uninstall") loadApps(); return; } const file = event.target.closest("[data-file-name]"); if (file) fileClicked(file); });

  async function renderFiles(path) {
    const body = document.getElementById("toolboxBody"); body.innerHTML = `<div class="tool-row"><input class="grow" id="remotePath" value="${esc(path)}"><button class="btn" id="goRemotePath">打开</button><button class="btn" id="mkdirRemote">新建文件夹</button><label class="btn primary">选择文件上传<input hidden type="file" id="pushFiles" multiple></label></div><div class="drop-zone" id="fileDrop">将电脑文件拖到这里，上传到当前手机目录</div><div class="file-list" id="fileList"><span class="muted">正在读取文件…</span></div>`;
    document.getElementById("goRemotePath").onclick = () => renderFiles(document.getElementById("remotePath").value);
    document.getElementById("mkdirRemote").onclick = async () => { const name = prompt("请输入文件夹名称"); if (!name) return; await apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/files/action`, {action:"mkdir",path:filePath,name}); renderFiles(filePath); };
    document.getElementById("pushFiles").onchange = event => uploadFiles(event.target.files);
    const drop = document.getElementById("fileDrop"); drop.ondragover = event => { event.preventDefault(); drop.classList.add("drag"); }; drop.ondragleave = () => drop.classList.remove("drag"); drop.ondrop = event => { event.preventDefault(); drop.classList.remove("drag"); uploadFiles(event.dataTransfer.files); };
    const data = await getJson(`/api/devices/${encodeURIComponent(toolboxSerial)}/files?path=${encodeURIComponent(path)}`); if (!data) return; filePath = data.path; document.getElementById("remotePath").value = filePath;
    const entries = []; if (data.parent) entries.push(`<div class="file-item"><span class="file-name">📁 ..</span><button class="btn" data-file-parent="${esc(data.parent)}">返回上级</button></div>`); entries.push(...data.entries.map(item => `<div class="file-item"><span class="file-name">${item.directory ? "📁" : "📄"} ${esc(item.name)}</span><button class="btn" data-file-name="${esc(item.name)}" data-directory="${item.directory ? "1" : "0"}">${item.directory ? "打开" : "下载"}</button><button class="btn danger" data-file-delete="${esc(item.name)}">删除</button></div>`)); document.getElementById("fileList").innerHTML = entries.join("") || '<span class="muted">空目录</span>';
    document.querySelector("[data-file-parent]")?.addEventListener("click", event => renderFiles(event.currentTarget.dataset.fileParent));
    document.querySelectorAll("[data-file-delete]").forEach(button => button.onclick = async () => { const target = filePath.replace(/\/$/,"") + "/" + button.dataset.fileDelete; if (!confirm("确定删除 " + target + " 吗？")) return; await apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/files/action`, {action:"delete",path:target}); renderFiles(filePath); });
  }
  function fileClicked(button) { const target = filePath.replace(/\/$/,"") + "/" + button.dataset.fileName; if (button.dataset.directory === "1") renderFiles(target); else { const a=document.createElement("a"); a.href=`/api/devices/${encodeURIComponent(toolboxSerial)}/files/download?path=${encodeURIComponent(target)}`; a.download=button.dataset.fileName; a.click(); } }
  async function uploadFiles(files) { for (const file of [...files]) await rawUpload(`/api/devices/${encodeURIComponent(toolboxSerial)}/files/upload?path=${encodeURIComponent(filePath)}&filename=${encodeURIComponent(file.name)}`, file); renderFiles(filePath); }

  function renderLogcat() { const body=document.getElementById("toolboxBody"); body.innerHTML=`<div class="tool-row"><button class="btn primary" id="startLogcat">开始</button><button class="btn" id="stopLogcat">停止</button><button class="btn" id="clearLogcat">清空</button><input class="grow" id="logcatFilter" placeholder="过滤日志关键字"></div><pre class="logcat-output" id="logcatOutput">等待启动 Logcat…</pre>`; document.getElementById("startLogcat").onclick=async()=>{await apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/logcat`,{action:"start"});pollLogcat();}; document.getElementById("stopLogcat").onclick=()=>apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/logcat`,{action:"stop"}); document.getElementById("clearLogcat").onclick=async()=>{await apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/logcat`,{action:"clear"});logcatLines=[];logcatOffset=0;drawLogcat();}; document.getElementById("logcatFilter").oninput=drawLogcat; pollLogcat(); }
  async function pollLogcat(){ if(!toolbox.classList.contains("show")||toolboxTab!=="logcat")return; const data=await getJson(`/api/devices/${encodeURIComponent(toolboxSerial)}/logcat?offset=${logcatOffset}`); if(data){if(data.reset)logcatLines=[];logcatLines.push(...data.lines);if(logcatLines.length>5000)logcatLines=logcatLines.slice(-5000);logcatOffset=data.offset;drawLogcat();} logcatTimer=setTimeout(pollLogcat,700); }
  function drawLogcat(){const output=document.getElementById("logcatOutput");if(!output)return;const filter=(document.getElementById("logcatFilter")?.value||"").toLowerCase();output.textContent=logcatLines.filter(line=>!filter||line.toLowerCase().includes(filter)).join("\n")||"暂无日志";output.scrollTop=output.scrollHeight;}

  function renderDebug(){document.getElementById("toolboxBody").innerHTML=`<div class="package-list"><div class="package-item"><div class="package-name"><b>可视化元素检查器</b><br><span class="muted">截图叠加 UIAutomator 元素边界，可筛选并点击控件</span></div><button class="btn primary" id="openInspector">打开检查器</button></div><div class="package-item"><div class="package-name"><b>页面元素日志</b><br><span class="muted">按设备和时间保存 UTF-8 元素日志</span></div><button class="btn" id="debugDump">立即打印</button></div><div class="package-item"><div class="package-name"><b>独立虚拟屏幕</b><br><span class="muted">通过 scrcpy --new-display 创建不占用手机主屏幕的独立显示</span></div><button class="btn" id="debugVirtual">创建虚拟屏幕</button></div></div>`;document.getElementById("openInspector").onclick=()=>openInspector(toolboxSerial);document.getElementById("debugDump").onclick=()=>apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/dump-ui`,{});document.getElementById("debugVirtual").onclick=()=>apiPost(`/api/devices/${encodeURIComponent(toolboxSerial)}/virtual-display`,{});}

  async function openInspector(serial){inspectorSerial=serial;inspector.classList.add("show");document.getElementById("inspectorInfo").textContent="正在抓取截图和元素…";const [data,blob]=await Promise.all([apiPost(`/api/devices/${encodeURIComponent(serial)}/inspect-ui`,{}),fetchScreenshot(serial)]).catch(error=>{toast(error.message,"err");return[]});if(!data||!data.ok||!blob)return;inspectorElements=data.elements||[];const image=document.getElementById("inspectorImage");const old=image.dataset.url;if(old)URL.revokeObjectURL(old);image.dataset.url=URL.createObjectURL(blob);image.src=image.dataset.url;image.onload=renderInspector;document.getElementById("inspectorInfo").textContent=`${data.count} 个元素 · ${data.filename}`;}
  function renderInspector(){const image=document.getElementById("inspectorImage"),stage=document.getElementById("inspectorStage");stage.querySelectorAll(".element-box").forEach(node=>node.remove());const w=image.naturalWidth,h=image.naturalHeight;inspectorElements.filter(item=>item.rect).forEach(item=>{const [x1,y1,x2,y2]=item.rect;const box=document.createElement("button");box.className="element-box"+(item.clickable?" clickable":"");box.style.cssText=`left:${x1/w*100}%;top:${y1/h*100}%;width:${Math.max(1,(x2-x1)/w*100)}%;height:${Math.max(1,(y2-y1)/h*100)}%`;box.title=item.text||item.resource_id||item.class_name||`#${item.index}`;box.dataset.elementIndex=item.index;stage.appendChild(box);});renderElementList();}
  function renderElementList(){const query=(document.getElementById("inspectorFilter").value||"").toLowerCase();const items=inspectorElements.filter(item=>!query||[item.text,item.resource_id,item.class_name,item.content_description].some(v=>String(v||"").toLowerCase().includes(query)));document.getElementById("inspectorList").innerHTML=items.slice(0,800).map(item=>`<div class="element-item" data-element-index="${item.index}"><b>#${item.index} ${esc(item.text||item.content_description||"(无文本)")}</b><br>${esc(item.resource_id||"-")}<br><span class="muted">${esc(item.class_name||"-")} ${item.clickable?"· clickable":""}</span></div>`).join("")||'<span class="muted">没有匹配元素</span>';}
  function selectElement(index){const item=inspectorElements.find(value=>String(value.index)===String(index));if(!item)return;document.querySelectorAll(".element-box").forEach(box=>box.classList.toggle("selected",box.dataset.elementIndex===String(index)));document.getElementById("inspectorSelected").textContent=`#${item.index} · ${item.text||item.content_description||"无文本"} · ${item.resource_id||"无 ID"} · ${item.class_name||""}`;const tap=document.getElementById("inspectorTap");tap.disabled=!item.rect;tap.dataset.elementIndex=item.index;}
  document.getElementById("inspectorStage").onclick=event=>{const box=event.target.closest("[data-element-index]");if(box)selectElement(box.dataset.elementIndex);};document.getElementById("inspectorList").onclick=event=>{const row=event.target.closest("[data-element-index]");if(row)selectElement(row.dataset.elementIndex);};document.getElementById("inspectorFilter").oninput=renderElementList;document.getElementById("inspectorTap").onclick=()=>{const item=inspectorElements.find(value=>String(value.index)===document.getElementById("inspectorTap").dataset.elementIndex);if(item?.rect){const [x1,y1,x2,y2]=item.rect;apiPost(`/api/devices/${encodeURIComponent(inspectorSerial)}/tap`,{x:Math.round((x1+x2)/2),y:Math.round((y1+y2)/2)});}};document.getElementById("inspectorRefresh").onclick=()=>openInspector(inspectorSerial);document.getElementById("inspectorClose").onclick=()=>inspector.classList.remove("show");

  const pvBar=document.querySelector("#pvModal .pv-bar");const recordStart=document.createElement("button");recordStart.className="btn";recordStart.id="recordStart";recordStart.textContent="开始录制";const recordLibrary=document.createElement("button");recordLibrary.className="btn";recordLibrary.textContent="录制库";pvBar.prepend(recordLibrary);pvBar.prepend(recordStart);
  function captureAction(action,body){if(!recording)return;const now=Date.now();const copy=JSON.parse(JSON.stringify(body||{}));if(["tap","swipe"].includes(action)){const source=latestDevices.find(item=>item.serial===streamSerial);const match=String(source?.resolution||"").match(/(\d+)\D+(\d+)/);if(match){copy.source_width=Number(match[1]);copy.source_height=Number(match[2]);}}recordedActions.push({delay:lastRecordedAt?now-lastRecordedAt:0,action,body:copy});lastRecordedAt=now;recordStart.textContent=`停止并保存 (${recordedActions.length})`;}
  const toolkitPreviewSend=previewSend;previewSend=function(url,body){captureAction(url.split("/").pop(),body);return toolkitPreviewSend(url,body);};
  document.getElementById("pvSendText").addEventListener("click",()=>captureAction("text",{text:document.getElementById("pvTextInput").value}));
  recordStart.onclick=async()=>{if(!recording){recording=true;recordedActions=[];lastRecordedAt=0;recordStart.classList.add("recording-live");recordStart.textContent="停止并保存 (0)";toast("操作录制已开始","ok");return;}recording=false;recordStart.classList.remove("recording-live");recordStart.textContent="开始录制";if(recordedActions.length)await apiPost("/api/recordings/save",{name:"操作录制 "+new Date().toLocaleString(),actions:recordedActions});};
  recordLibrary.onclick=()=>openRecords();recordsButton.onclick=()=>openRecords();document.getElementById("recordsClose").onclick=()=>recordsModal.classList.remove("show");document.getElementById("refreshRecords").onclick=loadRecords;document.getElementById("stopReplays").onclick=()=>apiPost("/api/replay/stop",{});
  async function openRecords(){recordsModal.classList.add("show");loadRecords();pollReplays();}
  async function loadRecords(){const data=await getJson("/api/recordings");if(!data)return;document.getElementById("recordList").innerHTML=data.recordings.length?data.recordings.map(item=>`<div class="record-item"><div class="record-name"><b>${esc(item.name)}</b><br><span class="muted">${esc(item.created_at)} · ${item.count||item.actions?.length||0} 个动作</span></div><button class="btn primary" data-replay="${item.id}">在已勾选设备回放</button><button class="btn danger" data-delete-record="${item.id}">删除</button></div>`).join(""):'<span class="muted">暂无录制，请从网页预览开始录制。</span>';}
  document.getElementById("recordList").onclick=async event=>{const play=event.target.closest("[data-replay]");if(play){const targets=onlineTargets();if(!targets.length)return toast("请先勾选在线设备","err");await apiPost("/api/replay/run",{recording_id:play.dataset.replay,serials:targets});pollReplays();}const del=event.target.closest("[data-delete-record]");if(del&&confirm("确定删除这条录制吗？")){await apiPost("/api/recordings/delete",{id:del.dataset.deleteRecord});loadRecords();}};
  async function pollReplays(){if(!recordsModal.classList.contains("show"))return;const data=await getJson("/api/replay");if(data)document.getElementById("replayStates").innerHTML=data.tasks.length?data.tasks.map(task=>`<div>${esc(task.serial)} · ${esc(task.name)} · ${esc(task.status)} · ${task.progress}/${task.total}${task.error?" · "+esc(task.error):""}</div>`).join(""):'暂无回放任务';setTimeout(pollReplays,900);}

  pluginsButton.onclick=()=>openPlugins();document.getElementById("pluginsClose").onclick=()=>pluginsModal.classList.remove("show");document.getElementById("refreshPlugins").onclick=loadPlugins;document.getElementById("runPlugins").onclick=async()=>{const targets=onlineTargets();if(!targets.length)return toast("请先勾选在线设备","err");await apiPost("/api/plugins/run",{serials:targets});pollPlugins();};document.getElementById("stopPlugins").onclick=()=>apiPost("/api/plugins/stop",{});
  function openPlugins(){pluginsModal.classList.add("show");loadPlugins();pollPlugins();}
  async function loadPlugins(){const data=await getJson("/api/plugins");if(!data)return;document.getElementById("pluginList").innerHTML=data.plugins.length?data.plugins.map(plugin=>`<label class="plugin-item"><input type="checkbox" data-plugin-toggle="${plugin.id}" ${plugin.enabled?"checked":""}><div class="plugin-name"><b>${esc(plugin.name)}</b> <span class="badge">v${esc(plugin.version)}</span><br><span class="muted">${esc(plugin.description)}</span></div></label>`).join(""):'<span class="muted">没有发现插件，请在 adb_manager/plugins 下添加插件目录。</span>';}
  document.getElementById("pluginList").onchange=async event=>{const checkbox=event.target.closest("[data-plugin-toggle]");if(checkbox)await apiPost("/api/plugins/toggle",{id:checkbox.dataset.pluginToggle,enabled:checkbox.checked});};
  async function pollPlugins(){if(!pluginsModal.classList.contains("show"))return;const data=await getJson("/api/plugins/tasks?offsets="+encodeURIComponent(JSON.stringify(pluginOffsets)));if(data){const blocks=[];data.tasks.forEach(task=>{pluginOffsets[task.id]=task.offset;blocks.push(`[${task.serial}] ${task.status} ${task.progress}/${task.total} ${task.current||""}${task.error?" · "+task.error:""}`,...task.lines);});if(blocks.length){const output=document.getElementById("pluginOutput");output.textContent+=(output.textContent==="暂无插件任务"?"":"\n")+blocks.join("\n");output.scrollTop=output.scrollHeight;}}setTimeout(pollPlugins,900);}
})();
