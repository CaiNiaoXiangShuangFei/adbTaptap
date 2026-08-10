(function () {
  "use strict";

  async function initializeDesktopBridge() {
    document.body.classList.add("desktop-app");
    const actions = document.querySelector(".head-actions");
    if (!actions || document.getElementById("desktopAlwaysTop")) return;

    const badge = document.createElement("span");
    badge.className = "badge online";
    badge.textContent = "桌面版";
    badge.title = "本地后端与内嵌 Edge WebView2";
    actions.insertBefore(badge, actions.firstChild);

    const topButton = document.createElement("button");
    topButton.id = "desktopAlwaysTop";
    topButton.className = "btn";
    actions.insertBefore(topButton, document.getElementById("btnSettings"));

    let state = {on_top: false};
    try {
      state = await window.pywebview.api.get_window_state();
    } catch (_) {
    }
    const render = () => {
      topButton.textContent = state.on_top ? "取消窗口置顶" : "窗口置顶";
      topButton.classList.toggle("primary", !!state.on_top);
    };
    render();
    topButton.onclick = async () => {
      try {
        const result = await window.pywebview.api.set_always_on_top(!state.on_top);
        state.on_top = !!result.on_top;
        render();
      } catch (error) {
        toast("桌面窗口设置失败：" + error, "err");
      }
    };
  }

  document.addEventListener("pywebviewready", initializeDesktopBridge);
  if (window.pywebview?.api) initializeDesktopBridge();
})();
