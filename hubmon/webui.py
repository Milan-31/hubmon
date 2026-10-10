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
            {"key": "card_glow", "label": "卡片底色律动", "type": "bool"},
            {"key": "bg_image", "label": "背景图片", "type": "text"},
            {"key": "bg_image_blur", "label": "图片模糊", "type": "num",
             "min": 0, "max": 40, "step": 1, "unit": "px"},
            {"key": "bg_image_brightness", "label": "图片亮度", "type": "num",
             "min": 0, "max": 2, "step": 0.05, "unit": "×"},
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
    --accent:#38bdf8; --accent-2:#a78bfa; --danger:#f87171; --success:#34d399;
    --warn:#fbbf24; --faint:#6b7385;
    --r-card:18px; --r-ctl:12px;
    --ease:cubic-bezier(.22,.7,.25,1);
    --shadow:0 10px 26px rgba(0,0,0,.34);
    --hi:inset 0 1px 0 rgba(255,255,255,.06);
    --font:-apple-system,"SF Pro Text","SF Pro Display","PingFang SC",
           "Microsoft YaHei UI","Segoe UI",system-ui,sans-serif;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;padding:0;height:100%}
  body{
    font-family:var(--font); color:var(--text); background:var(--bg);
    overflow:hidden;
    font-size:15px; line-height:1.45;
    -webkit-font-smoothing:antialiased;
    font-variant-numeric:tabular-nums;
    background-image:
      radial-gradient(120% 70% at 50% -10%, rgba(56,189,248,.10), transparent 60%),
      radial-gradient(90% 60% at 100% 0%, rgba(167,139,250,.07), transparent 55%);
    background-attachment:fixed;
  }
  svg{display:block}

  /* ---- app shell: pinned top area, then sidebar | content ---- */
  .app{
    display:flex;flex-direction:column;
    height:100vh;height:100dvh;
    max-width:1000px;margin:0 auto;
  }

  /* ---- entrance ---- */
  @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  .anim{animation:rise .5s cubic-bezier(.22,.7,.25,1) both}

  /* ---- pinned top area (brand row + stats) ---- */
  .topbar{
    flex:0 0 auto;z-index:15;padding:0 22px;
    background:linear-gradient(180deg,rgba(14,19,30,.95),rgba(11,15,23,.80));
    backdrop-filter:saturate(150%) blur(18px);
    border-bottom:1px solid var(--sep);
    box-shadow:0 1px 0 rgba(255,255,255,.03),0 10px 26px rgba(0,0,0,.28);
  }
  .top{display:flex;align-items:center;gap:14px;padding:15px 0 11px}
  .brand{display:flex;align-items:center;gap:13px;min-width:0}
  .brand-icon{
    width:44px;height:44px;flex:0 0 auto;border-radius:14px;
    display:flex;align-items:center;justify-content:center;color:var(--accent);
    background:linear-gradient(160deg,rgba(56,189,248,.24),rgba(56,189,248,.05));
    border:1px solid rgba(56,189,248,.30);
    box-shadow:0 6px 18px rgba(56,189,248,.16),inset 0 1px 0 rgba(255,255,255,.10);
  }
  .brand-icon svg{width:24px;height:24px}
  .brand h1{font-size:19px;font-weight:640;letter-spacing:.2px;margin:0}
  .brand p{margin:3px 0 0;font-size:12.5px;color:var(--muted);overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap;max-width:190px}
  .spacer{flex:1 1 auto}
  .pill{
    display:inline-flex;align-items:center;gap:7px;padding:7px 14px;border-radius:999px;
    font-size:12.5px;font-weight:560;color:var(--muted);
    background:rgba(255,255,255,.06);border:1px solid var(--card-line);
    white-space:nowrap;box-shadow:var(--hi);
    transition:color .25s,border-color .25s,background .25s,box-shadow .25s;
  }
  .pill .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);
    box-shadow:0 0 0 3px rgba(139,147,163,.12);transition:background .25s,box-shadow .25s}
  .pill.online{color:var(--success);border-color:rgba(52,211,153,.32);
    background:rgba(52,211,153,.10);
    box-shadow:0 4px 14px rgba(52,211,153,.15),inset 0 1px 0 rgba(255,255,255,.07)}
  .pill.online .dot{background:var(--success);box-shadow:0 0 0 3px rgba(52,211,153,.18);
    animation:beat 2.6s ease-in-out infinite}
  .pill.offline{color:var(--muted)}
  @keyframes beat{0%,100%{opacity:1}50%{opacity:.4}}

  /* ---- stats ---- */
  .stats{
    display:grid;grid-template-columns:repeat(6,minmax(0,1fr));
    gap:10px;padding-bottom:13px;
  }
  .stat{
    --c:#38bdf8;
    position:relative;min-width:0;overflow:hidden;
    background:var(--card);border:1px solid var(--card-line);border-radius:var(--r-card);
    padding:11px 13px 10px;
    background-image:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,0));
    box-shadow:var(--hi);transition:border-color .3s,box-shadow .3s;
  }
  .stat-label{
    display:flex;align-items:center;gap:6px;
    font-size:11px;letter-spacing:.4px;color:var(--muted);font-weight:560;
    white-space:nowrap;overflow:hidden;
  }
  .stat-label::before{
    content:"";flex:0 0 auto;width:6px;height:6px;border-radius:50%;
    background:var(--c);box-shadow:0 0 8px var(--c);opacity:.9;
  }
  .stat-num-row{display:flex;align-items:baseline;gap:4px;margin-top:7px}
  .stat-num{font-size:24px;font-weight:640;letter-spacing:-.4px;line-height:1;
    color:var(--c);transition:color .35s}
  .stat-unit{font-size:11px;font-style:normal;color:var(--muted)}
  .stat.warn{--c:#fbbf24}
  .stat.hot{--c:#f87171}

  /* ---- two-column body: sidebar | content ---- */
  .body{flex:1 1 auto;min-height:0;display:flex;padding:20px 22px 0}
  .side{
    flex:0 0 208px;display:flex;flex-direction:column;gap:3px;
    overflow-y:auto;padding:2px 18px 14px 0;border-right:1px solid var(--sep);
  }
  .navitem{
    position:relative;display:flex;align-items:center;gap:11px;width:100%;flex:0 0 auto;
    font-family:var(--font);font-size:14.5px;font-weight:560;color:var(--muted);
    background:transparent;border:1px solid transparent;border-radius:var(--r-ctl);
    padding:11px 13px;cursor:pointer;text-align:left;white-space:nowrap;
    transition:color .2s,background .2s,border-color .2s,box-shadow .2s,transform .2s var(--ease);
  }
  .navitem .navicon{width:20px;height:20px;flex:0 0 auto;display:flex;
    align-items:center;justify-content:center;transition:color .2s}
  .navitem .navicon svg{width:18px;height:18px}
  .navitem:hover{color:var(--text);background:rgba(255,255,255,.05)}
  .navitem:active{transform:scale(.985)}
  .navitem.on{
    color:var(--text);
    background:linear-gradient(90deg,rgba(56,189,248,.16),rgba(56,189,248,.04));
    border-color:rgba(56,189,248,.30);
    box-shadow:0 6px 18px rgba(56,189,248,.13),var(--hi);
  }
  .navitem.on .navicon{color:var(--accent)}
  .navitem.on::after{
    content:"";position:absolute;left:-1px;top:50%;transform:translateY(-50%);
    width:3px;height:18px;border-radius:0 3px 3px 0;background:var(--accent);
    box-shadow:0 0 10px rgba(56,189,248,.7);
  }
  .navitem:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(56,189,248,.30)}
  .content{
    flex:1 1 auto;min-width:0;min-height:0;overflow-y:auto;
    padding:2px 0 22px 22px;
  }
  .content-title{
    display:flex;align-items:center;gap:9px;margin:0 0 12px 2px;
    font-size:14px;font-weight:620;letter-spacing:.2px;color:var(--text);
  }
  .content-title svg{width:17px;height:17px;color:var(--accent)}
  /* deliberate, slim scrollbars */
  .side::-webkit-scrollbar,.content::-webkit-scrollbar{width:9px;height:9px}
  .side::-webkit-scrollbar-track,.content::-webkit-scrollbar-track{background:transparent}
  .side::-webkit-scrollbar-thumb,.content::-webkit-scrollbar-thumb{
    background:rgba(255,255,255,.13);border-radius:999px;
    border:3px solid transparent;background-clip:content-box;
  }
  .side::-webkit-scrollbar-thumb:hover,.content::-webkit-scrollbar-thumb:hover{
    background:rgba(255,255,255,.24);background-clip:content-box;
  }
  .card{
    background:var(--card);border:1px solid var(--card-line);border-radius:var(--r-card);
    overflow:hidden;backdrop-filter:saturate(150%) blur(2px);
    box-shadow:var(--shadow),var(--hi);
  }
  .row{position:relative;display:flex;align-items:center;gap:12px;padding:13px 16px;
    transition:background .2s}
  .row + .row::before{
    content:"";position:absolute;top:0;left:16px;right:16px;height:1px;background:var(--sep);
  }
  .row:hover{background:rgba(255,255,255,.028)}
  .row .label{font-size:15px;color:var(--text)}
  .row.row-col{flex-direction:column;align-items:stretch;gap:12px}
  .row-head{display:flex;align-items:center;gap:10px}
  .row-head .value{
    display:inline-flex;align-items:baseline;font-size:13px;font-weight:620;
    color:#bfe9ff;background:rgba(56,189,248,.12);border:1px solid rgba(56,189,248,.22);
    border-radius:999px;padding:3px 11px;font-variant-numeric:tabular-nums;
    box-shadow:var(--hi);transition:background .2s;
  }
  .row-head .value i{font-style:normal;font-size:11px;color:var(--muted);margin-left:4px}

  /* ---- toggle ---- */
  .switch{position:relative;display:inline-block;width:51px;height:31px;flex:0 0 auto}
  .switch input{position:absolute;opacity:0;width:0;height:0;margin:0}
  .switch .track{
    position:absolute;inset:0;border-radius:999px;background:rgba(255,255,255,.16);
    box-shadow:inset 0 1px 2px rgba(0,0,0,.28);
    transition:background .3s cubic-bezier(.4,0,.2,1),box-shadow .3s;
  }
  .switch .track::after{
    content:"";position:absolute;top:2px;left:2px;width:27px;height:27px;border-radius:50%;
    background:linear-gradient(180deg,#fff,#eef4fb);
    box-shadow:0 2px 6px rgba(0,0,0,.42),0 0 0 .5px rgba(0,0,0,.06);
    transition:transform .3s cubic-bezier(.4,0,.2,1),box-shadow .25s;
  }
  .switch:hover .track::after{
    box-shadow:0 2px 9px rgba(0,0,0,.5),0 0 0 .5px rgba(0,0,0,.06);
  }
  .switch input:checked + .track{
    background:linear-gradient(135deg,#38bdf8,#0ea5e9);
    box-shadow:0 4px 14px rgba(56,189,248,.35),inset 0 1px 0 rgba(255,255,255,.28);
  }
  .switch input:checked + .track::after{transform:translateX(20px)}
  .switch input:focus-visible + .track{box-shadow:0 0 0 3px rgba(56,189,248,.30)}

  /* ---- range: the bar is the input's own background-image, sized to 5px so it
     always reads as a slim track (gradient fill + dim remainder) ---- */
  input[type=range]{
    -webkit-appearance:none;appearance:none;width:100%;height:24px;margin:0;
    background-color:transparent;background-repeat:no-repeat;
    background-position:center;background-size:100% 5px;
    cursor:pointer;
  }
  input[type=range]:focus-visible{outline:none}
  input[type=range]::-webkit-slider-runnable-track{
    height:5px;border-radius:999px;background:transparent;
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none;appearance:none;width:20px;height:20px;border-radius:50%;
    margin-top:-7.5px;
    background:radial-gradient(circle at 50% 32%,#ffffff,#e8f3ff 68%,#d8e9fb);
    box-shadow:0 2px 7px rgba(0,0,0,.5),0 0 0 .5px rgba(0,0,0,.14),
      0 0 0 4px rgba(56,189,248,.10);
    transition:transform .16s var(--ease),box-shadow .22s;
  }
  input[type=range]:hover::-webkit-slider-thumb{
    box-shadow:0 3px 9px rgba(0,0,0,.5),0 0 0 .5px rgba(0,0,0,.14),
      0 0 0 6px rgba(56,189,248,.20);
  }
  input[type=range]:active::-webkit-slider-thumb{transform:scale(1.10)}
  input[type=range]:focus-visible::-webkit-slider-thumb{
    box-shadow:0 2px 8px rgba(0,0,0,.5),0 0 0 6px rgba(56,189,248,.32);
  }
  input[type=range]::-moz-range-track{
    height:5px;border-radius:999px;background:rgba(255,255,255,.11);
  }
  input[type=range]::-moz-range-progress{
    height:5px;border-radius:999px;background:linear-gradient(90deg,#5ecbff,#0ea5e9);
  }
  input[type=range]::-moz-range-thumb{
    width:19px;height:19px;border:none;border-radius:50%;
    background:radial-gradient(circle at 50% 32%,#ffffff,#e8f3ff 68%,#d8e9fb);
    box-shadow:0 2px 7px rgba(0,0,0,.5);
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
  /* long values (e.g. an executable path) need room to stay readable */
  .row-wide input.input{width:min(420px,58%);text-align:left}

  /* ---- action bar ---- */
  .actionbar{
    flex:0 0 auto;
    display:flex;justify-content:center;gap:12px;
    padding:13px 22px calc(13px + env(safe-area-inset-bottom));
    background:rgba(11,15,23,.92);
    backdrop-filter:blur(14px);
    border-top:1px solid var(--sep);
  }
  .actionbar-inner{display:flex;gap:12px;width:100%}
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
    border-color:var(--card-line);box-shadow:var(--hi);
  }
  .btn.ghost:hover{background:rgba(255,255,255,.13)}
  .btn.ghost:active{background:rgba(255,255,255,.09)}
  .btn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(56,189,248,.40)}
  .btn.primary:focus-visible{
    box-shadow:0 8px 22px rgba(56,189,248,.32),inset 0 1px 0 rgba(255,255,255,.35),
      0 0 0 3px rgba(56,189,248,.40);
  }

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

  @media (max-width:820px){
    .topbar{padding:0 14px}
    .brand h1{font-size:18px}
    /* stats collapse to one swipeable row so the pinned area stays short */
    .stats{
      display:flex;gap:9px;overflow-x:auto;padding-bottom:12px;
      scrollbar-width:none;-webkit-overflow-scrolling:touch;
    }
    .stats::-webkit-scrollbar{display:none}
    .stat{flex:0 0 auto;min-width:98px}
    .stat-num{font-size:22px}
    /* sidebar becomes a horizontal switcher above the pane */
    .body{flex-direction:column;padding:14px 14px 0}
    .side{
      flex:0 0 auto;flex-direction:row;overflow-x:auto;overflow-y:hidden;
      gap:7px;padding:0 0 10px 0;border-right:none;
      border-bottom:1px solid var(--sep);scrollbar-width:none;
    }
    .side::-webkit-scrollbar{display:none}
    .navitem{width:auto;padding:9px 13px;font-size:13.5px}
    .navitem.on::after{display:none}
    .content{padding:12px 0 16px 0}
    /* long-value rows stack so the value gets the full width */
    .row-wide{flex-wrap:wrap}
    .row-wide .spacer{display:none}
    .row-wide .label{white-space:nowrap}
    .row-wide input.input{flex:1 1 100%;width:100%;max-width:none}
    .actionbar{padding:12px 14px calc(12px + env(safe-area-inset-bottom))}
  }
  @media (prefers-reduced-motion:reduce){
    *{animation-duration:.001ms !important;transition-duration:.001ms !important}
  }
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <header class="top">
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
  </div>

  <div class="body">
    <nav class="side" id="nav" aria-label="功能分类"></nav>
    <main class="content" id="content"></main>
  </div>

  <div class="actionbar">
    <div class="actionbar-inner">
      <button class="btn ghost" id="quit" type="button">退出程序</button>
      <button class="btn ghost" id="reset" type="button">恢复默认</button>
      <button class="btn primary" id="save" type="button">保存设置</button>
    </div>
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
  var active = 0;

  /* one colour identity per metric, mirroring the screen; load metrics warn
     (amber) then alarm (red) as they climb */
  var STATS = [
    {k:'cpu',     label:'CPU',     unit:'%',   d:1, c:'#38bdf8', warn:80,  hot:92},
    {k:'gpu',     label:'GPU',     unit:'%',   d:1, c:'#a78bfa', warn:80,  hot:92},
    {k:'mem',     label:'内存',    unit:'%',   d:0, c:'#22d3ee', warn:80,  hot:92},
    {k:'power',   label:'整机功率', unit:'W',   d:0, c:'#fbbf24', warn:200, hot:270},
    {k:'fg_fps',  label:'前台帧率', unit:'FPS', d:0, c:'#34d399'},
    {k:'out_fps', label:'推流帧率', unit:'FPS', d:1, c:'#34d399'}
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
    input.style.backgroundImage =
      'linear-gradient(to right,#5ecbff 0%,#0ea5e9 ' + pct +
      '%,rgba(255,255,255,.11) ' + pct + '%,rgba(255,255,255,.11) 100%)';
  }

  function repaintRanges(){
    var rs = document.querySelectorAll('#content input[type=range]');
    for(var i=0;i<rs.length;i++){ paintRange(rs[i]); }
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
    el.className = row.type === 'port' ? 'row' : 'row row-wide';
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

  function buildNav(){
    var root = $('#nav');
    root.innerHTML = '';
    SCHEMA.forEach(function(g, gi){
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'navitem anim' + (gi === active ? ' on' : '');
      b.setAttribute('data-idx', gi);
      if(gi === active){ b.setAttribute('aria-current', 'true'); }
      b.style.animationDelay = (gi * 35) + 'ms';
      b.innerHTML = '<span class="navicon">' + (g.icon || '') + '</span>' +
        '<span class="navlabel">' + esc(g.title) + '</span>';
      b.addEventListener('click', function(){ select(gi); });
      root.appendChild(b);
    });
  }

  function buildContent(){
    var root = $('#content');
    root.innerHTML = '';
    var g = SCHEMA[active];
    if(!g){ return; }
    var head = document.createElement('div');
    head.className = 'content-title anim';
    head.innerHTML = (g.icon || '') + '<span>' + esc(g.title) + '</span>';
    var card = document.createElement('div');
    card.className = 'card anim';
    g.rows.forEach(function(row){ card.appendChild(buildRow(row)); });
    root.appendChild(head);
    root.appendChild(card);
  }

  function select(i){
    active = i;
    var items = document.querySelectorAll('.navitem');
    for(var k=0;k<items.length;k++){
      var on = Number(items[k].getAttribute('data-idx')) === i;
      items[k].classList.toggle('on', on);
      if(on){ items[k].setAttribute('aria-current', 'true'); }
      else { items[k].removeAttribute('aria-current'); }
    }
    buildContent();
    $('#content').scrollTop = 0;
  }

  function buildStats(){
    var root = $('#stats');
    root.innerHTML = '';
    STATS.forEach(function(s, i){
      var d = document.createElement('div');
      d.className = 'stat anim';
      d.id = 'stat-' + s.k;
      d.style.animationDelay = (i * 35) + 'ms';
      if(s.c){ d.style.setProperty('--c', s.c); }
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
      var v = Number(stats[s.k]);
      $('.stat-num', el).textContent = num(stats[s.k], s.d);
      var hot = isFinite(v) && s.hot != null && v >= s.hot;
      var warn = !hot && isFinite(v) && s.warn != null && v >= s.warn;
      el.classList.toggle('hot', hot);
      el.classList.toggle('warn', warn);
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
    buildContent();
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
    buildNav();
    buildContent();
    poll();
    setInterval(poll, 1000);
    window.addEventListener('resize', repaintRanges);
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