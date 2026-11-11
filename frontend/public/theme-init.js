// 首屏渲染前定好主题，避免深色模式下先闪一下浅色。放成外部文件是因为后端的 CSP 不允许内联脚本。
try {
  var t = localStorage.getItem("solo-auto-theme");
  if (t !== "dark" && t !== "light") t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = t;
} catch (e) {}
