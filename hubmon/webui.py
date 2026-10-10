"""Local web control panel for hubmon.

Serves a self-contained, single-page iOS-style dashboard over an embedded
stdlib HTTP server bound to 127.0.0.1. The panel edits every field of
``hubmon.config.Config`` live (the render loop reads the same object) and
shows real-time stats pulled from a provider callable.

Only the standard library is used.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# Form schema: one source of truth for both the backend and the injected JS.
# --------------------------------------------------------------------------- #
_ICON_STREAM = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="2"/>'
    '<path d="M16.2 7.8a6 6 0 0 1 0 8.4"/><path d="M7.8 16.2a6 6 0 0 1 0-8.4"/>'
    '<path d="M19 4.9a10 10 0 0 1 0 14.2"/><path d="M5 19.1a10 10 0 0 1 0-14.2"/></svg>'
)
_ICON_TEXT = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4 7V5h16v2"/>'
    '<path d="M12 5v14"/><path d="M9 19h6"/></svg>'
)
_ICON_BG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 9 5-9 5-9-5 9-5Z"/>'
    '<path d="m3 13 9 5 9-5"/></svg>'
)
_ICON_LOOK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 3a9 9 0 1 0 0 18h1.5a2 2 0 0 0 0-4H13a2 2 0 0 1 0-4h3a5 5 0 0 0 '
    '0-10h-4Z"/><circle cx="7.5" cy="10.5" r=".9"/><circle cx="12" cy="7.5" r=".9"/>'
    '<circle cx="16.5" cy="10.5" r=".9"/></svg>'
)
_ICON_PANEL = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16"/><path d="M4 12h16"/>'
    '<path d="M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/>'
    '<circle cx="8" cy="18" r="2"/></svg>'
)
_ICON_TOOL = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/>'
    '<path d="M12 2.5v2.6M12 18.9v2.6M21.5 12h-2.6M5.1 12H2.5M18.7 5.3l-1.8 1.8'
    'M7.1 16.9l-1.8 1.8M18.7 18.7l-1.8-1.8M7.1 7.1 5.3 5.3"/></svg>'
)

SCHEMA = [
    {
        "title": "推流",
        "icon": _ICON_STREAM,
        "rows": [
            {"key": "fps", "label": "帧率上限", "type": "num",
             "min": 0, "max": 120, "step": 1, "unit": "fps"},
            {"key": "partial", "label": "局部更新", "type": "bool"},
            {"key": "text_period", "label": "数值刷新间隔", "type": "num",
             "min": 0.1, "max": 10, "step": 0.1, "unit": "s"},
            {"key": "fps_period", "label": "帧率刷新间隔", "type": "num",
             "min": 0.1, "max": 5, "step": 0.1, "unit": "s"},
        ],
    },
    {
        "title": "文字动效",
        "icon": _ICON_TEXT,
        "rows": [
            {"key": "roll", "label": "数值滚动", "type": "bool"},
            {"key": "roll_speed", "label": "滚动速度", "type": "num",
             "min": 0.5, "max": 10, "step": 0.1, "unit": "次/s"},
        ],
    },
    {
        "title": "动态背景",
        "icon": _ICON_BG,
        "rows": [
            {"key": "bg_motion", "label": "背景动效", "type": "bool"},
            {"key": "bg_speed", "label": "动效速度", "type": "num",
             "min": 0, "max": 5, "step": 0.1, "unit": "×"},
            {"key": "particles", "label": "粒子数量", "type": "num",
             "min": 0, "max": 80, "step": 1, "unit": "个"},
            {"key": "blooms", "label": "光晕数量", "type": "num",
             "min": 0, "max": 8, "step": 1, "unit": "个"},
        ],
    },
    {
        "title": "外观",
        "icon": _ICON_LOOK,
        "rows": [
            {"key": "brightness", "label": "亮度", "type": "num",
             "min": 0, "max": 2, "step": 0.05, "unit": "×"},
            {"key": "bg_brightness", "label": "背景亮度", "type": "num",
             "min": 0, "max": 1.5, "step": 0.05, "unit": "×"},
            {"key": "saturation", "label": "饱和度", "type": "num",
             "min": 0, "max": 2, "step": 0.05, "unit": "×"},
            {"key": "glass", "label": "玻璃质感", "type": "num",
             "min": 0, "max": 2, "step": 0.05, "unit": "×"},
            {"key": "accent_cpu", "label": "CPU 强调色", "type": "color"},
            {"key": "accent_gpu", "label": "GPU 强调色", "type": "color"},
            {"key": "accent_mem", "label": "内存强调色", "type": "color"},
            {"key": "accent_pwr", "label": "功率强调色", "type": "color"},
            {"key": "show_mem", "label": "显示内存", "type": "bool"},
            {"key": "show_power", "label": "显示功率", "type": "bool"},
            {"key": "show_fps", "label": "显示帧率", "type": "bool"},
        ],
    },
    {
        "title": "面板",
        "icon": _ICON_PANEL,
        "rows": [
            {"key": "ui_port", "label": "面板端口", "type": "port"},
            {"key": "open_browser", "label": "启动时打开浏览器", "type": "bool"},
        ],
    },
    {
        "title": "外部工具",
        "icon": _ICON_TOOL,
        "rows": [
            {"key": "presentmon_path", "label": "PresentMon 路径", "type": "text"},
        ],
    },
]


# --------------------------------------------------------------------------- #
# HTML page (self-contained; the three __TOKENS__ are filled at request time)
# --------------------------------------------------------------------------- #
_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>系统监测器</title>
<style>
  :root{
    --bg:#0b0f17; --card:rgba(255,255,255,.06); --card-line:rgba(255,255,255,.09);
    --sep:rgba(255,255,255,.06); --text:#f4f7fc; --muted:#8b93a3;
    --accent:#38bdf8; --danger:#f87171; --success:#34d399;
    --r-card:18px; --r-ctl:12px;
    --font:-apple-system,"SF Pro Text","SF Pro Display","PingFang SC",
           "Microsoft YaHei UI","Segoe UI",system-ui,sans-serif;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;padding:0}
  body{
    font-family:var(--font); color:var(--text); background:var(--bg);
    min-height:100vh; padding:0 18px 108px;
    font-size:15px; line-height:1.45;
    -webkit-font-smoothing:antialiased;
    font-variant-numeric:tabular-nums;
    background-image:
      radial-gradient(120% 70% at 50% -10%, rgba(56,189,248,.10), transparent 60%),
      radial-gradient(90% 60% at 100% 0%, rgba(167,139,250,.07), transparent 55%);
    background-attachment:fixed;
  }
  .wrap{max-width:720px;margin:0 auto}
  svg{display:block}

  /* ---- entrance ---- */
  @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  .anim{animation:rise .5s cubic-bezier(.22,.7,.25,1) both}

  /* ---- top bar ---- */
  .top{
    position:sticky; top:0; z-index:15;
    display:flex;align-items:center;gap:14px;
    padding:18px 0 13px; margin-bottom:18px;
    background:rgba(11,15,23,.86);
    backdrop-filter:saturate(140%) blur(16px);
    border-bottom:1px solid var(--sep);
  }
  .brand{display:flex;align-items:center;gap:13px;min-width:0}
  .brand-icon{
    width:44px;height:44px;flex:0 0 auto;border-radius:14px;
    display:flex;align-items:center;justify-content:center;color:var(--accent);
    background:linear-gradient(160deg,rgba(56,189,248,.22),rgba(56,189,248,.06));
    border:1px solid rgba(56,189,248,.28);
    box-shadow:0 6px 18px rgba(56,189,248,.14);
  }
  .brand-icon svg{width:24px;height:24px}
  .brand h1{font-size:20px;font-weight:650;letter-spacing:.2px;margin:0}
  .brand p{margin:2px 0 0;font-size:12.5px;color:var(--muted);overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap;max-width:180px}
  .spacer{flex:1 1 auto}
  .pill{
    display:inline-flex;align-items:center;gap:7px;padding:7px 13px;border-radius:999px;
    font-size:12.5px;font-weight:560;color:var(--muted);
    background:rgba(255,255,255,.06);border:1px solid var(--card-line);
    white-space:nowrap;transition:color .25s,border-color .25s,background .25s;
  }
  .pill .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);
    box-shadow:0 0 0 3px rgba(139,147,163,.12);transition:background .25s,box-shadow .25s}
  .pill.online{color:var(--success);border-color:rgba(52,211,153,.3);
    background:rgba(52,211,153,.09)}
  .pill.online .dot{background:var(--success);box-shadow:0 0 0 3px rgba(52,211,153,.16)}
  .pill.offline{color:var(--muted)}

  /* ---- stats ---- */
  .stats{
    display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
    gap:12px;margin-bottom:26px;
  }
  .stat{
    background:var(--card);border:1px solid var(--card-line);border-radius:var(--r-card);
    padding:14px 15px 13px;
    background-image:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,0));
  }
  .stat-label{font-size:11.5px;letter-spacing:.5px;color:var(--muted);font-weight:560}
  .stat-num-row{display:flex;align-items:baseline;gap:4px;margin-top:8px}
  .stat-num{font-size:27px;font-weight:640;letter-spacing:-.4px;line-height:1}
  .stat-unit{font-size:12px;font-style:normal;color:var(--muted)}

  /* ---- groups ---- */
  .group{margin-bottom:24px}
  .group-title{
    display:flex;align-items:center;gap:7px;margin:0 0 9px 6px;
    font-size:11.5px;font-weight:600;letter-spacing:1.3px;text-transform:uppercase;
    color:var(--muted);
  }
  .group-title svg{width:15px;height:15px;opacity:.85}
  .card{
    background:var(--card);border:1px solid var(--card-line);border-radius:var(--r-card);
    overflow:hidden;backdrop-filter:saturate(140%);
  }
  .row{position:relative;display:flex;align-items:center;gap:12px;padding:12px 15px}
  .row + .row::before{
    content:"";position:absolute;top:0;left:15px;right:0;height:1px;background:var(--sep);
  }
  .row .label{font-size:15px;color:var(--text)}
  .row.row-col{flex-direction:column;align-items:stretch;gap:11px}
  .row-head{display:flex;align-items:baseline;gap:10px}
  .row-head .value{font-size:14px;color:var(--accent);font-weight:600;font-variant-numeric:tabular-nums}
  .row-head .value i{font-style:normal;font-size:11.5px;color:var(--muted);margin-left:3px}

  /* ---- toggle ---- */
  .switch{position:relative;display:inline-block;width:51px;height:31px;flex:0 0 auto}
  .switch input{position:absolute;opacity:0;width:0;height:0;margin:0}
  .switch .track{
    position:absolute;inset:0;border-radius:999px;background:rgba(255,255,255,.16);
    transition:background .3s cubic-bezier(.4,0,.2,1);
  }
  .switch .track::after{
    content:"";position:absolute;top:2px;left:2px;width:27px;height:27px;border-radius:50%;
    background:#fff;box-shadow:0 2px 6px rgba(0,0,0,.42),0 0 0 .5px rgba(0,0,0,.05);
    transition:transform .3s cubic-bezier(.4,0,.2,1);
  }
  .switch input:checked + .track{background:linear-gradient(135deg,#38bdf8,#0ea5e9);
    box-shadow:0 4px 14px rgba(56,189,248,.35)}
  .switch input:checked + .track::after{transform:translateX(20px)}
  .switch input:focus-visible + .track{box-shadow:0 0 0 3px rgba(56,189,248,.3)}

  /* ---- range ---- */
  input[type=range]{
    -webkit-appearance:none;appearance:none;width:100%;height:22px;margin:0;
    background:transparent;cursor:pointer;
  }
  input[type=range]::-webkit-slider-runnable-track{
    height:4px;border-radius:999px;
    background:rgba(255,255,255,.14);
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none;appearance:none;width:20px;height:20px;border-radius:50%;
    margin-top:-8px;background:#fff;
    box-shadow:0 1px 5px rgba(0,0,0,.5),0 0 0 .5px rgba(0,0,0,.12);
    transition:transform .15s cubic-bezier(.4,0,.2,1);
  }
  input[type=range]:active::-webkit-slider-thumb{transform:scale(1.14)}
  input[type=range]::-moz-range-track{
    height:4px;border-radius:999px;background:rgba(255,255,255,.14);
  }
  input[type=range]::-moz-range-progress{
    height:4px;border-radius:999px;background:linear-gradient(90deg,#38bdf8,#22a9e6);
  }
  input[type=range]::-moz-range-thumb{
    width:18px;height:18px;border:none;border-radius:50%;background:#fff;
    box-shadow:0 1px 5px rgba(0,0,0,.5);
  }

  /* ---- color ---- */
  input[type=color]{
    -webkit-appearance:none;appearance:none;width:28px;height:28px;padding:0;border:none;
    border-radius:50%;background:none;cursor:pointer;
  }
  input[type=color]::-webkit-color-swatch-wrapper{padding:0}
  input[type=color]::-webkit-color-swatch{border:1px solid rgba(255,255,255,.28);border-radius:50%}
  input[type=color]::-moz-color-swatch{border:1px solid rgba(255,255,255,.28);border-radius:50%}

  /* ---- text / port ---- */
  input.input{
    font-family:var(--font);font-size:14.5px;color:var(--text);
    background:rgba(0,0,0,.28);border:1px solid var(--card-line);border-radius:var(--r-ctl);
    padding:8px 12px;width:150px;text-align:right;outline:none;
    transition:border-color .2s,box-shadow .2s;font-variant-numeric:tabular-nums;
  }
  input.input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(56,189,248,.2)}

  /* ---- action bar ---- */
  .actionbar{
    position:fixed;left:0;right:0;bottom:0;z-index:20;
    display:flex;justify-content:center;gap:12px;
    padding:14px 18px calc(14px + env(safe-area-inset-bottom));
    background:linear-gradient(180deg,rgba(11,15,23,0),rgba(11,15,23,.86) 42%,rgba(11,15,23,.97));
    backdrop-filter:blur(14px);
  }
  .actionbar-inner{display:flex;gap:12px;width:100%;max-width:720px}
  .btn{
    font-family:var(--font);font-size:15px;font-weight:600;color:var(--text);
    border-radius:999px;border:1px solid transparent;cursor:pointer;
    padding:13px 22px;transition:transform .18s cubic-bezier(.4,0,.2,1),filter .2s,
      background .2s,box-shadow .2s;
  }
  .btn:active{transform:scale(.965)}
  .btn.primary{
    flex:1 1 auto;color:#04121c;border:none;
    background:linear-gradient(135deg,#5ecbff,#38bdf8 45%,#0ea5e9);
    box-shadow:0 8px 22px rgba(56,189,248,.32),inset 0 1px 0 rgba(255,255,255,.35);
  }
  .btn.primary:hover{filter:brightness(1.06)}
  .btn.primary[disabled]{opacity:.6;pointer-events:none}
  .btn.ghost{
    flex:0 0 auto;color:var(--text);background:rgba(255,255,255,.07);
    border-color:var(--card-line);
  }
  .btn.ghost:hover{background:rgba(255,255,255,.12)}

  /* ---- toast ---- */
  #toast{
    position:fixed;left:50%;bottom:96px;transform:translate(-50%,14px);
    padding:10px 18px;border-radius:999px;font-size:13.5px;font-weight:560;
    color:var(--text);background:rgba(30,38,52,.96);border:1px solid var(--card-line);
    box-shadow:0 10px 30px rgba(0,0,0,.5);
    opacity:0;pointer-events:none;transition:opacity .28s,transform .28s;
    z-index:30;white-space:nowrap;
  }
  #toast.show{opacity:1;transform:translate(-50%,0)}

  @media (max-width:560px){
    body{padding:0 13px 100px}
    .brand h1{font-size:18px}
    .stat-num{font-size:23px}
    .stats{grid-template-columns:repeat(auto-fit,minmax(88px,1fr));gap:10px}
    input.input{width:120px}
  }
  @media (prefers-reduced-motion:reduce){
    *{animation-duration:.001ms !important;transition-duration:.001ms !important}
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="top anim">
    <div class="brand">
      <div class="brand-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
             stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="4" width="18" height="12" rx="2.4"/>
          <path d="M8 20h8"/><path d="M12 16v4"/>
          <path d="M7 11l2.4-2.4L11.6 11 15 7.4"/>
        </svg>
      </div>
      <div>
        <h1>系统监测器</h1>
        <p id="host">本机</p>
      </div>
    </div>
    <div class="spacer"></div>
    <div class="pill" id="status"><span class="dot"></span><span id="status-text">连接中</span></div>
  </header>

  <div class="stats" id="stats"></div>
  <div id="form"></div>
</div>

<div class="actionbar">
  <div class="actionbar-inner">
    <button class="btn ghost" id="quit" type="button">退出程序</button>
    <button class="btn ghost" id="reset" type="button">恢复默认</button>
    <button class="btn primary" id="save" type="button">保存设置</button>
  </div>
</div>

<div id="toast"></div>

<script>
(function(){
  "use strict";
  var DEFAULTS = __DEFAULTS_JSON__;
  var CFG = __CONFIG_JSON__;
  var SCHEMA = __SCHEMA_JSON__;
  var stats = {};
  var toastTimer = null;

  var STATS = [
    {k:'cpu',     label:'CPU',    unit:'%',   d:1},
    {k:'gpu',     label:'GPU',    unit:'%',   d:1},
    {k:'mem',     label:'内存',   unit:'%',   d:0},
    {k:'power',   label:'整机功率', unit:'W',   d:0},
    {k:'fg_fps',  label:'前台帧率', unit:'FPS', d:0},
    {k:'out_fps', label:'推流帧率', unit:'FPS', d:1}
  ];

  function $(s, r){ return (r||document).querySelector(s); }
  function decimals(step){
    var s = String(step), i = s.indexOf('.');
    return i < 0 ? 0 : s.length - i - 1;
  }
  function num(v, d){
    if(v === null || v === undefined || isNaN(Number(v))) return '—';
    return Number(v).toFixed(d);
  }
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

  function paintRange(input){
    var min = Number(input.min), max = Number(input.max), v = Number(input.value);
    var pct = max > min ? ((v - min) / (max - min)) * 100 : 0;
    input.style.background = 'linear-gradient(to right,#38bdf8 0%,#38bdf8 ' + pct +
      '%,rgba(255,255,255,.14) ' + pct + '%,rgba(255,255,255,.14) 100%)';
  }

  function buildRow(row){
    var el, input;
    if(row.type === 'bool'){
      el = document.createElement('div');
      el.className = 'row';
      el.innerHTML = '<span class="label">' + esc(row.label) + '</span>' +
        '<div class="spacer"></div>' +
        '<label class="switch"><input type="checkbox"><span class="track"></span></label>';
      input = $('input', el);
      input.checked = !!CFG[row.key];
      input.addEventListener('change', function(){ CFG[row.key] = input.checked; });
      return el;
    }
    if(row.type === 'num'){
      el = document.createElement('div');
      el.className = 'row row-col';
      el.innerHTML = '<div class="row-head"><span class="label">' + esc(row.label) +
        '</span><span class="value"><em data-val></em><i>' + esc(row.unit || '') +
        '</i></span></div><input type="range" min="' + row.min + '" max="' + row.max +
        '" step="' + row.step + '">';
      input = $('input', el);
      var valEl = $('[data-val]', el);
      var d = decimals(row.step);
      input.value = Number(CFG[row.key]);
      var upd = function(){
        valEl.textContent = num(input.value, d);
        paintRange(input);
      };
      input.addEventListener('input', function(){
        CFG[row.key] = Number(input.value); upd();
      });
      upd();
      window.addEventListener('resize', function(){ paintRange(input); });
      return el;
    }
    if(row.type === 'color'){
      el = document.createElement('div');
      el.className = 'row';
      el.innerHTML = '<span class="label">' + esc(row.label) + '</span>' +
        '<div class="spacer"></div><input type="color">';
      input = $('input', el);
      input.value = CFG[row.key] || '#ffffff';
      input.addEventListener('input', function(){ CFG[row.key] = input.value; });
      return el;
    }
    /* port / text */
    el = document.createElement('div');
    el.className = 'row';
    var itype = row.type === 'port' ? 'number' : 'text';
    var extra = row.type === 'port' ? ' min="1" max="65535" step="1" inputmode="numeric"' : '';
    el.innerHTML = '<span class="label">' + esc(row.label) + '</span>' +
      '<div class="spacer"></div><input class="input" type="' + itype + '"' + extra + '>';
    input = $('input', el);
    input.value = CFG[row.key];
    input.addEventListener('input', function(){
      CFG[row.key] = row.type === 'port'
        ? (parseInt(input.value || '0', 10) || 0)
        : input.value;
    });
    return el;
  }

  function buildForm(){
    var root = $('#form');
    root.innerHTML = '';
    SCHEMA.forEach(function(g, gi){
      var sec = document.createElement('section');
      sec.className = 'group anim';
      sec.style.animationDelay = (gi * 45) + 'ms';
      var title = document.createElement('div');
      title.className = 'group-title';
      title.innerHTML = (g.icon || '') + '<span>' + esc(g.title) + '</span>';
      var card = document.createElement('div');
      card.className = 'card';
      g.rows.forEach(function(row){ card.appendChild(buildRow(row)); });
      sec.appendChild(title);
      sec.appendChild(card);
      root.appendChild(sec);
    });
  }

  function buildStats(){
    var root = $('#stats');
    root.innerHTML = '';
    STATS.forEach(function(s, i){
      var d = document.createElement('div');
      d.className = 'stat anim';
      d.id = 'stat-' + s.k;
      d.style.animationDelay = (i * 35) + 'ms';
      d.innerHTML = '<div class="stat-label">' + esc(s.label) + '</div>' +
        '<div class="stat-num-row"><span class="stat-num">—</span>' +
        '<i class="stat-unit">' + esc(s.unit) + '</i></div>';
      root.appendChild(d);
    });
  }

  function renderStats(){
    STATS.forEach(function(s){
      var el = document.getElementById('stat-' + s.k);
      if(!el) return;
      $('.stat-num', el).textContent = num(stats[s.k], s.d);
    });
    var hostEl = $('#host');
    if(stats.host){ hostEl.textContent = stats.host; }
  }

  function renderStatus(){
    var pill = $('#status');
    var on = !!stats.connected;
    pill.classList.toggle('online', on);
    pill.classList.toggle('offline', !on);
    $('#status-text').textContent = on ? '已连接' : '未连接';
  }

  function toast(msg){
    var t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    if(toastTimer){ clearTimeout(toastTimer); }
    toastTimer = setTimeout(function(){ t.classList.remove('show'); }, 1600);
  }

  function poll(){
    fetch('/api/stats', {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(d){ stats = d || {}; renderStats(); renderStatus(); })
      .catch(function(){});
  }

  function save(){
    var btn = $('#save');
    btn.disabled = true;
    var label = btn.textContent;
    fetch('/api/config', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(CFG)
    }).then(function(r){ return r.json(); })
      .then(function(d){
        if(d && d.ok){
          if(d.config){ CFG = d.config; }
          btn.textContent = '已保存';
          toast('设置已保存');
          setTimeout(function(){ btn.textContent = label; }, 1200);
        } else {
          toast('保存失败');
        }
      })
      .catch(function(){ toast('保存失败'); })
      .then(function(){ btn.disabled = false; });
  }

  function reset(){
    CFG = JSON.parse(JSON.stringify(DEFAULTS));
    buildForm();
    toast('已恢复默认（未保存）');
  }

  function quit(){
    if(!window.confirm('确定退出监测器？屏幕会停止刷新。')){ return; }
    fetch('/api/quit', {method:'POST'})
      .then(function(){ toast('监测器已退出'); })
      .catch(function(){ toast('退出失败'); });
  }

  document.addEventListener('DOMContentLoaded', function(){
    buildStats();
    buildForm();
    poll();
    setInterval(poll, 1000);
    $('#save').addEventListener('click', save);
    $('#reset').addEventListener('click', reset);
    $('#quit').addEventListener('click', quit);
  });
})();
</script>
</body>
</html>
"""


def _embed(obj) -> str:
    """JSON-encode for safe inlining inside a <script> block."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _render_page(cfg) -> str:
    from .config import Config

    defaults = Config().to_dict()
    schema = [
        {"title": g["title"], "icon": g["icon"], "rows": g["rows"]} for g in SCHEMA
    ]
    return (
        _PAGE
        .replace("__DEFAULTS_JSON__", _embed(defaults))
        .replace("__CONFIG_JSON__", _embed(cfg.to_dict()))
        .replace("__SCHEMA_JSON__", _embed(schema))
    )


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Explicitly off so a busy port raises instead of silently sharing it,
    # which lets start() fall through to port+1 ... on Windows too.
    allow_reuse_address = False

    cfg = None
    provider = None
    actual_port = None
    on_quit = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "hubmon"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # keep the console clean
    def log_message(self, *args):  # noqa: D401
        pass

    def log_error(self, *args):
        pass

    # -------------------------------------------------------------- response
    def _respond(self, code, body: bytes, ctype: str):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._respond(code, body, "application/json; charset=utf-8")

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/":
                html = _render_page(self.server.cfg).encode("utf-8")
                self._respond(200, html, "text/html; charset=utf-8")
            elif path == "/api/config":
                self._json(200, self.server.cfg.to_dict())
            elif path == "/api/stats":
                data = self.server.provider()
                self._json(200, data if isinstance(data, dict) else {})
            elif path == "/api/status":
                self._json(200, {"ok": True, "port": self.server.actual_port})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # never let the handler thread die
            self._json(500, {"ok": False, "error": str(exc)})

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if path == "/api/config":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                self.server.cfg.update_from(body)
                self.server.cfg.save()
                self._json(200, {"ok": True, "config": self.server.cfg.to_dict()})
            elif path == "/api/quit":
                self._json(200, {"ok": True})
                cb = self.server.on_quit
                if cb is not None:
                    # let the response reach the browser before we tear down
                    threading.Timer(0.35, cb).start()
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def start(cfg, provider, port=None, open_browser=False, on_quit=None):
    """Start the panel on 127.0.0.1 and return the running server."""
    base = int(port if port is not None else getattr(cfg, "ui_port", 8756))

    httpd = None
    actual = None
    last_err = None
    for candidate in range(base, base + 11):  # port .. port+10
        try:
            httpd = _Server(("127.0.0.1", candidate), _Handler)
            actual = candidate
            break
        except OSError as exc:
            last_err = exc
            continue

    if httpd is None:
        raise OSError(
            f"no free port in {base}..{base + 10}: {last_err}"
        )

    httpd.cfg = cfg
    httpd.provider = provider
    httpd.actual_port = actual
    httpd.on_quit = on_quit

    thread = threading.Thread(
        target=httpd.serve_forever, name="hubmon-webui", daemon=True
    )
    thread.start()
    httpd._thread = thread

    if open_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{actual}/")
        except Exception:
            pass

    return httpd


def stop(server):
    """Shut the panel down."""
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass