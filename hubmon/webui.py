"""Local web control panel for hubmon.

Serves a self-contained, single-page iOS-style dashboard over an embedded
stdlib HTTP server bound to 127.0.0.1. The panel edits every field of
``hubmon.config.Config`` live (the render loop reads the same object) and
shows real-time stats pulled from a provider callable.

Only the standard library is used.
"""
from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


# --------------------------------------------------------------------------- #
# Native "open image" dialog (Windows), so the panel can hand the choice over
# to Explorer instead of asking the user to type a path.
# --------------------------------------------------------------------------- #
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            # Vista+ tail: lStructSize has to match the full modern struct or
            # the shell rejects the call outright
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]


def pick_image_path():
    """Show the Windows open-file dialog; return the chosen path or ''."""
    if os.name != "nt":
        return ""
    try:
        OFN_FILEMUSTEXIST, OFN_PATHMUSTEXIST = 0x00001000, 0x00000800
        OFN_EXPLORER, OFN_NOCHANGEDIR = 0x00080000, 0x00000008

        buf = ctypes.create_unicode_buffer(1024)
        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = None
        ofn.lpstrFilter = ("图片文件\0*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp\0"
                           "所有文件\0*.*\0\0")
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
        ofn.nMaxFile = len(buf)
        ofn.lpstrTitle = "选择背景图片"
        ofn.Flags = (OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER
                     | OFN_NOCHANGEDIR)
        if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return ""
        return buf.value
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Form schema: one source of truth for both the backend and the injected JS.
# --------------------------------------------------------------------------- #
_ICON_STREAM = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="2"/>'
    '<path d="M16.2 7.8a6 6 0 0 1 0 8.4"/><path d="M7.8 16.2a6 6 0 0 1 0-8.4"/>'
    '<path d="M19 4.9a10 10 0 0 1 0 14.2"/><path d="M5 19.1a10 10 0 0 1 0-14.2"/></svg>'
)
_ICON_STYLE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M4.6 19.4 15.4 8.6"/>'
    '<path d="m15.3 3.4.9 2.4 2.4.9-2.4.9-.9 2.4-.9-2.4-2.4-.9 2.4-.9.9-2.4Z"/>'
    '<path d="m8.1 3.2.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4L6.2 5.1l1.4-.5.5-1.4Z"/></svg>'
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
# The app icon: a dark tile carrying a lit spectrum, served as the panel's
# favicon. Source of truth is assets/hubmon.svg; assets/make_icon.py draws the
# same figure for the executable's .ico. Small enough that inlining it keeps the
# frozen build self-contained -- no asset file to bundle.
_ICON_APP = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs>'
    '<linearGradient id="t" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#1a2331"/><stop offset="1" stop-color="#0a0e16"/>'
    '</linearGradient>'
    '<linearGradient id="s" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#fff" stop-opacity=".16"/>'
    '<stop offset=".55" stop-color="#fff" stop-opacity="0"/>'
    '</linearGradient>'
    '<linearGradient id="b" x1="0" y1="1" x2="0" y2="0">'
    '<stop offset="0" stop-color="#38bdf8"/><stop offset="1" stop-color="#a78bfa"/>'
    '</linearGradient>'
    '</defs>'
    '<rect width="64" height="64" rx="15" fill="url(#t)"/>'
    '<rect width="64" height="64" rx="15" fill="url(#s)"/>'
    '<rect x=".75" y=".75" width="62.5" height="62.5" rx="14.25" fill="none"'
    ' stroke="#fff" stroke-opacity=".14" stroke-width="1.5"/>'
    '<rect x="7" y="18" width="50" height="28" rx="5.5" fill="#fff" fill-opacity=".04"/>'
    '<rect x="12" y="42.2" width="40" height="1.6" rx=".8" fill="#fff" fill-opacity=".22"/>'
    '<g fill="url(#b)">'
    '<rect x="9" y="33" width="7" height="10" rx="1.8"/>'
    '<rect x="19" y="26" width="7" height="17" rx="1.8"/>'
    '<rect x="29" y="36" width="7" height="7" rx="1.8"/>'
    '<rect x="39" y="29" width="7" height="14" rx="1.8"/>'
    '<rect x="49" y="31" width="7" height="12" rx="1.8"/>'
    '</g>'
    '<g fill="#fff" fill-opacity=".95">'
    '<rect x="9" y="33" width="7" height="1.6" rx=".8"/>'
    '<rect x="19" y="26" width="7" height="1.6" rx=".8"/>'
    '<rect x="29" y="36" width="7" height="1.6" rx=".8"/>'
    '<rect x="39" y="29" width="7" height="1.6" rx=".8"/>'
    '<rect x="49" y="31" width="7" height="1.6" rx=".8"/>'
    '</g>'
    '</svg>'
)
# One-click palettes. A preset rewrites the four metric accents and the three
# gradient stops together, which is the fiddly bit users otherwise hand-tune one
# swatch at a time. "default" repeats today's colours exactly, so an existing
# config shows no change until another preset is actually chosen.
THEMES = {
    "default": ("冷夜蓝", {
        "accent_cpu": "#38bdf8", "accent_gpu": "#a78bfa",
        "accent_mem": "#22d3ee", "accent_pwr": "#fbbf24",
        "bg_color_top": "#0a0e16", "bg_color_mid": "#0d121c",
        "bg_color_bot": "#101522"}),
    "graphite": ("石墨灰", {
        "accent_cpu": "#94a3b8", "accent_gpu": "#cbd5e1",
        "accent_mem": "#a1a1aa", "accent_pwr": "#d4d4d8",
        "bg_color_top": "#0b0b0d", "bg_color_mid": "#111114",
        "bg_color_bot": "#17171b"}),
    "ember": ("暗焰橙", {
        "accent_cpu": "#fb923c", "accent_gpu": "#f472b6",
        "accent_mem": "#facc15", "accent_pwr": "#f87171",
        "bg_color_top": "#170f0b", "bg_color_mid": "#20130d",
        "bg_color_bot": "#2a1810"}),
    "aurora": ("极光青", {
        "accent_cpu": "#2dd4bf", "accent_gpu": "#818cf8",
        "accent_mem": "#4ade80", "accent_pwr": "#a3e635",
        "bg_color_top": "#061110", "bg_color_mid": "#0a1a19",
        "bg_color_bot": "#0d2320"}),
}


def _theme_row():
    return {
        "key": "theme", "label": "配色方案", "type": "enum",
        "options": [[k, v[0]] for k, v in THEMES.items()],
        "presets": {k: v[1] for k, v in THEMES.items()},
    }


# Groups carry ``sections`` rather than a flat row list: a group of a dozen
# settings reads as an undifferentiated wall otherwise, and the caption is what
# lets related controls stay together instead of splitting into their own nav
# entry. ``when`` gates a section on another setting (see the backdrop modes).
SCHEMA = [
    {
        "title": "主题",
        "icon": _ICON_STYLE,
        "sections": [
            {"caption": "风格与配色", "rows": [
                {"key": "style", "label": "界面风格", "type": "enum",
                 "options": [["glass", "玻璃拟态"], ["quartz", "石英电子"]]},
                _theme_row(),
                {"key": "accent_cpu", "label": "CPU 强调色", "type": "color"},
                {"key": "accent_gpu", "label": "GPU 强调色", "type": "color"},
                {"key": "accent_mem", "label": "内存强调色", "type": "color"},
                {"key": "accent_pwr", "label": "功率强调色", "type": "color"},
            ]},
        ],
    },
    {
        "title": "画面",
        "icon": _ICON_LOOK,
        "sections": [
            {"caption": "显示模式", "rows": [
                {"key": "mode", "label": "显示模式", "type": "enum",
                 "options": [["dash", "指标仪表盘"], ["spectrum", "音频频谱"]]},
            ]},
            {"caption": "音频频谱", "when": {"key": "mode", "eq": "spectrum"},
             "rows": [
                 {"key": "spectrum_style", "label": "频谱风格", "type": "list",
                  "options": [["led", "LED 点阵柱"], ["peak", "峰值保持"],
                              ["line", "折线波形"], ["area", "面积山形"],
                              ["sym", "对称镜像柱"], ["grad", "单侧渐变柱"],
                              ["dots", "圆点脉冲"], ["fine", "细柱阵列"]]},
                 {"key": "spectrum_device", "label": "采集设备", "type": "list",
                  "remote": "/api/spectrum-devices"},
                 {"key": "spectrum_tilt", "label": "频段倾斜", "type": "num",
                  "min": -6, "max": 6, "step": 0.5, "unit": "dB/oct"},
                 {"key": "spectrum_smoothing", "label": "灵敏度平滑", "type": "num",
                  "min": 0, "max": 1, "step": 0.05, "unit": ""},
             ]},
            {"caption": "歌词（需在 QQ 音乐开启 SMTC 并完全重启客户端）", "when": {"key": "mode", "eq": "spectrum"},
             "rows": [
                 {"key": "lyrics_enable", "label": "显示歌词", "type": "bool"},
                 {"key": "lyrics_effect", "label": "歌词动效", "type": "list",
                  "options": [["fade", "渐隐"], ["geom", "几何重构"],
                              ["rotate", "轮换"]]},
                 {"key": "lyrics_opacity", "label": "歌词不透明度", "type": "num",
                  "min": 0.3, "max": 1.0, "step": 0.05, "unit": ""},
             ]},
            {"caption": "显示调节", "rows": [
                {"key": "brightness", "label": "整体亮度", "type": "num",
                 "min": 0, "max": 2, "step": 0.05, "unit": "×"},
                {"key": "saturation", "label": "饱和度", "type": "num",
                 "min": 0, "max": 2, "step": 0.05, "unit": "×"},
                {"key": "glass", "label": "玻璃质感", "type": "num",
                 "min": 0, "max": 2, "step": 0.05, "unit": "×"},
                {"key": "bg_brightness", "label": "背景亮度", "type": "num",
                 "min": 0, "max": 1.5, "step": 0.05, "unit": "×"},
            ]},
            {"caption": "指标显隐", "rows": [
                {"key": "show_mem", "label": "显示内存", "type": "bool"},
                {"key": "show_power", "label": "显示功率", "type": "bool"},
                {"key": "show_fps", "label": "显示帧率", "type": "bool"},
                {"key": "show_watt", "label": "卡片内瓦数", "type": "bool"},
            ]},
            {"caption": "屏幕顶栏", "rows": [
                {"key": "top_left", "label": "左上角内容", "type": "enum",
                 "options": [["board", "主板型号"], ["host", "主机名"]]},
                {"key": "clock_24h", "label": "24 小时制", "type": "bool"},
                {"key": "clock_seconds", "label": "时钟显示秒", "type": "bool"},
                {"key": "show_net", "label": "显示网速", "type": "bool"},
            ]},
        ],
    },
    {
        "title": "背景",
        "icon": _ICON_BG,
        "sections": [
            {"caption": "来源", "rows": [
                {"key": "bg_mode", "label": "背景来源", "type": "enum",
                 "options": [["auto", "自动"], ["gradient", "纯渐变"],
                             ["image", "图片"]]},
            ]},
            {"caption": "渐变", "when": {"key": "bg_mode", "neq": "image"},
             "rows": [
                 {"key": "bg_color_top", "label": "顶部颜色", "type": "color"},
                 {"key": "bg_color_mid", "label": "中部颜色", "type": "color"},
                 {"key": "bg_color_bot", "label": "底部颜色", "type": "color"},
             ]},
            {"caption": "图片", "when": {"key": "bg_mode", "neq": "gradient"},
             "rows": [
                 {"key": "bg_image", "label": "背景图片", "type": "file"},
                 {"key": "bg_image_blur", "label": "图片模糊", "type": "num",
                  "min": 0, "max": 40, "step": 1, "unit": "px"},
                 {"key": "bg_image_brightness", "label": "图片亮度", "type": "num",
                  "min": 0, "max": 2, "step": 0.05, "unit": "×"},
             ]},
        ],
    },
    {
        "title": "动效与推流",
        "icon": _ICON_STREAM,
        "sections": [
            {"caption": "推流", "rows": [
                {"key": "fps", "label": "帧率上限", "type": "num",
                 "min": 0, "max": 120, "step": 1, "unit": "fps"},
                {"key": "partial", "label": "局部更新", "type": "bool"},
                {"key": "text_period", "label": "数值刷新间隔", "type": "num",
                 "min": 0.1, "max": 10, "step": 0.1, "unit": "s"},
                {"key": "fps_period", "label": "帧率刷新间隔", "type": "num",
                 "min": 0.1, "max": 5, "step": 0.1, "unit": "s"},
            ]},
            {"caption": "文字动效", "rows": [
                {"key": "roll", "label": "数值滚动", "type": "bool"},
                {"key": "roll_speed", "label": "滚动速度", "type": "num",
                 "min": 0.5, "max": 10, "step": 0.1, "unit": "次/s"},
            ]},
        ],
    },
    {
        "title": "系统",
        "icon": _ICON_PANEL,
        "sections": [
            {"caption": "", "rows": [
                {"key": "ui_port", "label": "面板端口", "type": "port"},
                {"key": "open_browser", "label": "启动时打开浏览器", "type": "bool"},
                {"key": "presentmon_path", "label": "PresentMon 路径", "type": "text"},
            ]},
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
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<style>
  :root{
    --bg:#0b0f17; --text:#f4f7fc; --muted:#8b93a3;
    --accent:#38bdf8; --danger:#f87171; --success:#34d399; --warn:#fbbf24;
    --faint:#6b7385;
    /* one raised surface and one hairline; everything below is built from them */
    --surface:rgba(255,255,255,.045);
    --hair:rgba(255,255,255,.055);
    /* Light neumorphism is a deep outer shadow paired with a single top
       highlight. The classic pale outer shadow is invisible on a near-black
       backdrop, so only those two halves of it survive -- which is the look
       wanted anyway. Two elevations only: raised (--e1/--e2) and sunken. */
    --e1:0 1px 2px rgba(0,0,0,.45),0 8px 20px -6px rgba(0,0,0,.55);
    --e2:0 2px 4px rgba(0,0,0,.5),0 16px 36px -10px rgba(0,0,0,.62);
    --hi:inset 0 1px 0 rgba(255,255,255,.07);
    --sink:inset 0 2px 5px rgba(0,0,0,.5),inset 0 -1px 0 rgba(255,255,255,.05);
    --r-lg:20px; --r-md:14px; --r-sw:11px; --r-pill:999px;
    --ease:cubic-bezier(.22,.7,.25,1);
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
    background:var(--bg);
    border-bottom:1px solid var(--hair);
    box-shadow:0 12px 26px -16px rgba(0,0,0,.75);
  }
  .top{display:flex;align-items:center;gap:14px;padding:15px 0 11px}
  .brand{display:flex;align-items:center;gap:13px;min-width:0}
  /* a raised tile rather than a glowing badge: the accent lives in the glyph */
  .brand-icon{
    width:44px;height:44px;flex:0 0 auto;border-radius:var(--r-md);
    display:flex;align-items:center;justify-content:center;color:var(--accent);
    background:var(--surface);border:1px solid var(--hair);
    box-shadow:var(--e1),var(--hi);
  }
  .brand-icon svg{width:24px;height:24px}
  .brand h1{font-size:19px;font-weight:640;letter-spacing:.2px;margin:0}
  .brand p{margin:3px 0 0;font-size:12.5px;color:var(--muted);overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap;max-width:190px}
  .spacer{flex:1 1 auto}
  .pill{
    display:inline-flex;align-items:center;gap:7px;padding:7px 14px;
    border-radius:var(--r-pill);
    font-size:12.5px;font-weight:560;color:var(--muted);
    background:var(--surface);border:1px solid var(--hair);
    white-space:nowrap;box-shadow:var(--hi);
    transition:color .25s,border-color .25s,background .25s;
  }
  .pill .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);
    transition:background .25s}
  .pill.online{color:var(--success);border-color:rgba(52,211,153,.30);
    background:rgba(52,211,153,.09)}
  .pill.online .dot{background:var(--success);
    animation:beat 2.6s ease-in-out infinite}
  .pill.offline{color:var(--muted)}
  @keyframes beat{0%,100%{opacity:1}50%{opacity:.4}}

  /* ---- stats: the same raised tile as everywhere else, no glow ---- */
  .stats{
    display:grid;grid-template-columns:repeat(6,minmax(0,1fr));
    gap:10px;padding-bottom:13px;
  }
  .stat{
    --c:#38bdf8;
    position:relative;min-width:0;overflow:hidden;
    background:var(--surface);border:1px solid var(--hair);
    border-radius:var(--r-md);
    padding:11px 13px 10px;
    box-shadow:var(--e1),var(--hi);transition:border-color .3s;
  }
  .stat-label{
    display:flex;align-items:center;gap:6px;
    font-size:11px;letter-spacing:.4px;color:var(--muted);font-weight:560;
    white-space:nowrap;overflow:hidden;
  }
  .stat-label::before{
    content:"";flex:0 0 auto;width:6px;height:6px;border-radius:50%;
    background:var(--c);opacity:.9;
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
    overflow-y:auto;padding:2px 18px 14px 0;border-right:1px solid var(--hair);
  }
  .navitem{
    position:relative;display:flex;align-items:center;gap:11px;width:100%;flex:0 0 auto;
    font-family:var(--font);font-size:14.5px;font-weight:560;color:var(--muted);
    background:transparent;border:1px solid transparent;border-radius:var(--r-md);
    padding:11px 13px;cursor:pointer;text-align:left;white-space:nowrap;
    transition:color .2s,background .2s,border-color .2s,transform .2s var(--ease);
  }
  .navitem .navicon{width:20px;height:20px;flex:0 0 auto;display:flex;
    align-items:center;justify-content:center;transition:color .2s}
  .navitem .navicon svg{width:18px;height:18px}
  .navitem:hover{color:var(--text);background:rgba(255,255,255,.04)}
  .navitem:active{transform:scale(.985)}
  /* the active entry is a sunken well with one accent rail -- no glow and no
     gradient fill, which was where most of the accent budget used to go */
  .navitem.on{
    color:var(--text);
    background:var(--surface);
    border-color:var(--hair);
    box-shadow:var(--sink);
  }
  .navitem.on .navicon{color:var(--accent)}
  .navitem.on::after{
    content:"";position:absolute;left:-1px;top:50%;transform:translateY(-50%);
    width:3px;height:18px;border-radius:0 3px 3px 0;background:var(--accent);
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
    background:var(--surface);border:1px solid var(--hair);
    border-radius:var(--r-lg);
    overflow:hidden;
    box-shadow:var(--e2),var(--hi);
  }
  /* caption above a group's controls: it is what lets a dozen related rows stay
     in one nav entry instead of splitting into their own. Padded to line up with
     the row labels below it. */
  .card-cap{
    padding:13px 16px 0;font-size:11.5px;font-weight:580;letter-spacing:.6px;
    color:var(--faint);
  }
  .card + .card{margin-top:18px}
  .card[hidden]{display:none}
  .row{position:relative;display:flex;align-items:center;gap:12px;padding:13px 16px;
    transition:background .2s}
  .row + .row::before{
    content:"";position:absolute;top:0;left:16px;right:16px;height:1px;background:var(--hair);
  }
  .row:hover{background:rgba(255,255,255,.022)}
  .row .label{font-size:15px;color:var(--text)}
  .row.row-col{flex-direction:column;align-items:stretch;gap:12px}
  .row-head{display:flex;align-items:center;gap:10px}
  /* a neutral raised pill: the accent is reserved for the nav rail, the primary
     button and the checked states */
  .row-head .value{
    display:inline-flex;align-items:baseline;font-size:13px;font-weight:620;
    color:var(--text);background:var(--surface);border:1px solid var(--hair);
    border-radius:var(--r-pill);padding:3px 11px;font-variant-numeric:tabular-nums;
    box-shadow:var(--hi);
  }
  .row-head .value i{font-style:normal;font-size:11px;color:var(--muted);margin-left:4px}

  /* ---- toggle: a sunken well with a physically raised knob; solid accent
     when on, no gradient and no glow ---- */
  .switch{position:relative;display:inline-block;width:51px;height:31px;flex:0 0 auto}
  .switch input{position:absolute;opacity:0;width:0;height:0;margin:0}
  .switch .track{
    position:absolute;inset:0;border-radius:var(--r-pill);
    background:rgba(0,0,0,.28);
    box-shadow:var(--sink);
    transition:background .3s cubic-bezier(.4,0,.2,1);
  }
  .switch .track::after{
    content:"";position:absolute;top:2px;left:2px;width:27px;height:27px;border-radius:50%;
    background:linear-gradient(180deg,#fff,#e9f1fa);
    box-shadow:0 2px 6px rgba(0,0,0,.5),0 0 0 .5px rgba(0,0,0,.12);
    transition:transform .3s cubic-bezier(.4,0,.2,1),box-shadow .25s;
  }
  .switch:hover .track::after{
    box-shadow:0 3px 10px rgba(0,0,0,.58),0 0 0 .5px rgba(0,0,0,.12);
  }
  .switch input:checked + .track{background:var(--accent)}
  .switch input:checked + .track::after{transform:translateX(20px)}
  .switch input:focus-visible + .track{
    outline:2px solid rgba(56,189,248,.55);outline-offset:2px;
  }

  /* ---- range: a sunken 5px groove with a raised knob. The filled part is the
     input's own background-image (see paintRange), kept flat on purpose ---- */
  input[type=range]{
    -webkit-appearance:none;appearance:none;width:100%;height:24px;margin:0;
    background-color:transparent;background-repeat:no-repeat;
    background-position:center;background-size:100% 5px;
    cursor:pointer;border-radius:var(--r-pill);
  }
  input[type=range]:focus-visible{outline:none}
  input[type=range]::-webkit-slider-runnable-track{
    height:5px;border-radius:var(--r-pill);background:transparent;
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none;appearance:none;width:19px;height:19px;border-radius:50%;
    margin-top:-7px;
    background:linear-gradient(180deg,#ffffff,#e6f0fa);
    box-shadow:0 2px 6px rgba(0,0,0,.55),0 0 0 .5px rgba(0,0,0,.16);
    transition:transform .16s var(--ease),box-shadow .22s;
  }
  input[type=range]:hover::-webkit-slider-thumb{
    box-shadow:0 3px 9px rgba(0,0,0,.6),0 0 0 .5px rgba(0,0,0,.16);
  }
  input[type=range]:active::-webkit-slider-thumb{transform:scale(1.10)}
  input[type=range]:focus-visible::-webkit-slider-thumb{
    box-shadow:0 2px 6px rgba(0,0,0,.55),0 0 0 3px rgba(56,189,248,.45);
  }
  input[type=range]::-moz-range-track{
    height:5px;border-radius:var(--r-pill);background:rgba(255,255,255,.10);
  }
  input[type=range]::-moz-range-progress{
    height:5px;border-radius:var(--r-pill);background:#38bdf8;
  }
  input[type=range]::-moz-range-thumb{
    width:19px;height:19px;border:none;border-radius:50%;
    background:linear-gradient(180deg,#ffffff,#e6f0fa);
    box-shadow:0 2px 6px rgba(0,0,0,.55);
  }

  /* ---- segmented control: capsule buttons in a sunken track; the picked one
     is a neutral raised chip carrying the accent in its text only ---- */
  .seg{
    display:inline-flex;gap:3px;padding:3px;border-radius:var(--r-pill);
    flex:0 0 auto;
    background:rgba(0,0,0,.28);box-shadow:var(--sink);
  }
  .seg-btn{
    font-family:var(--font);font-size:13px;font-weight:560;color:var(--muted);
    padding:6px 15px;border:none;border-radius:var(--r-pill);cursor:pointer;
    background:transparent;white-space:nowrap;
    transition:color .2s,background .25s,box-shadow .25s;
  }
  .seg-btn:hover{color:var(--text)}
  .seg-btn.on{
    color:var(--accent);background:var(--surface);
    box-shadow:var(--e1),var(--hi);
  }
  .seg-btn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(56,189,248,.30)}

  /* ---- colour: a swatch that opens an HSV picker popover ---- */
  .swatch{
    width:34px;height:34px;padding:0;border-radius:var(--r-sw);cursor:pointer;
    border:1px solid rgba(255,255,255,.20);background:none;
    box-shadow:0 2px 8px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.18);
    transition:transform .12s ease,box-shadow .16s ease;
  }
  .swatch:hover{
    transform:translateY(-1px);
    box-shadow:0 4px 12px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.26);
  }
  .swatch i{display:block;width:100%;height:100%;border-radius:calc(var(--r-sw) - 1px)}
  .hexval{
    font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums;
    letter-spacing:.3px;margin-right:11px;
  }
  .picker{
    position:fixed;z-index:90;width:236px;padding:14px;display:none;
    border-radius:var(--r-lg);border:1px solid var(--hair);
    background:rgba(17,21,29,.96);
    box-shadow:var(--e2);
  }
  .picker.open{display:block}
  .picker-sv{
    position:relative;height:130px;border-radius:var(--r-md);cursor:crosshair;
    box-shadow:inset 0 0 0 1px rgba(255,255,255,.16);
  }
  .picker-dot{
    position:absolute;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;
    border:2px solid #fff;pointer-events:none;
    box-shadow:0 0 0 1px rgba(0,0,0,.45),0 1px 5px rgba(0,0,0,.6);
  }
  .picker-hue{
    position:relative;height:13px;border-radius:var(--r-pill);margin-top:12px;
    cursor:pointer;
    background:linear-gradient(to right,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00);
    box-shadow:inset 0 0 0 1px rgba(255,255,255,.16);
  }
  .picker-hue i{
    position:absolute;top:50%;width:17px;height:17px;margin:-8.5px 0 0 -8.5px;
    border-radius:50%;background:#fff;pointer-events:none;
    box-shadow:0 0 0 1px rgba(0,0,0,.35),0 1px 5px rgba(0,0,0,.6);
  }
  .picker-hex{
    width:100%;margin-top:12px;font-family:var(--font);font-size:13.5px;
    color:var(--text);background:rgba(0,0,0,.3);border:1px solid var(--hair);
    border-radius:var(--r-sw);padding:8px 10px;outline:none;
    font-variant-numeric:tabular-nums;letter-spacing:.4px;
  }
  .picker-hex:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(56,189,248,.18)}
  .picker-presets{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
  .picker-presets button{
    width:21px;height:21px;padding:0;border-radius:7px;cursor:pointer;
    border:1px solid rgba(255,255,255,.2);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.28);
    transition:transform .12s ease;
  }
  .picker-presets button:hover{transform:translateY(-1px)}

  /* ---- text / port: a sunken field ---- */
  input.input{
    font-family:var(--font);font-size:14.5px;color:var(--text);
    background:rgba(0,0,0,.28);border:1px solid var(--hair);
    border-radius:var(--r-sw);
    padding:8px 12px;width:150px;text-align:right;outline:none;
    transition:border-color .2s,box-shadow .2s;font-variant-numeric:tabular-nums;
  }
  input.input:focus{border-color:var(--accent);box-shadow:var(--sink)}
  /* ---- pickers: a native select, so eight options fit where a segmented
     control would have run off the row ---- */
  select.input{
    font-family:var(--font);font-size:14px;color:var(--text);
    background-color:rgba(0,0,0,.28);border:1px solid var(--hair);
    border-radius:var(--r-sw);padding:8px 30px 8px 12px;
    width:min(260px,58%);text-align:left;outline:none;appearance:none;
    text-overflow:ellipsis;
    background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
                     linear-gradient(135deg,var(--muted) 50%,transparent 50%);
    background-position:calc(100% - 16px) 55%, calc(100% - 11px) 55%;
    background-size:5px 5px,5px 5px;background-repeat:no-repeat;
    transition:border-color .2s,box-shadow .2s;
  }
  select.input:focus{border-color:var(--accent);box-shadow:var(--sink)}
  select.input option{background:#1b1d22;color:var(--text)}
  /* long values (e.g. an executable path) need room to stay readable */
  .row-wide input.input{width:min(420px,58%);text-align:left}

  /* ---- file picker: show the chosen path, choose it through Explorer ---- */
  .row-wide .path{
    font-size:13px;color:var(--muted);max-width:min(300px,44%);
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    direction:rtl;text-align:left;
  }
  .row-wide .path.empty{color:var(--faint);direction:ltr}
  .btn.small{
    padding:9px 15px;font-size:13.5px;font-weight:580;border-radius:var(--r-pill);
    color:var(--text);background:var(--surface);
    border:1px solid var(--hair);box-shadow:var(--hi);
  }
  .btn.small:hover{background:rgba(255,255,255,.10)}
  .btn.small.ghost{color:var(--muted)}
  .btn.small.ghost:hover{color:var(--text);background:rgba(255,255,255,.10)}
  .btn.small[disabled]{opacity:.55;pointer-events:none}

  /* ---- crop preview: drag to pan, slider to zoom, 320:170 window ---- */
  .crop{display:flex;flex-direction:column;gap:11px}
  .crop-view{
    position:relative;overflow:hidden;width:100%;max-width:320px;
    aspect-ratio:320/170;border-radius:var(--r-md);background:rgba(0,0,0,.35);
    border:1px solid var(--hair);
    cursor:grab;touch-action:none;
    box-shadow:var(--sink);
  }
  .crop-view:active{cursor:grabbing}
  .crop-view img{
    position:absolute;left:0;top:0;transform-origin:top left;
    user-select:none;-webkit-user-drag:none;pointer-events:none;
  }
  .crop-ctl{display:flex;align-items:center;gap:12px;max-width:420px}
  .crop-ctl input[type=range]{flex:1 1 auto}
  .crop-ctl .value{
    display:inline-flex;align-items:baseline;font-size:13px;font-weight:620;
    color:var(--text);background:var(--surface);border:1px solid var(--hair);
    border-radius:var(--r-pill);padding:3px 11px;
    font-variant-numeric:tabular-nums;box-shadow:var(--hi);white-space:nowrap;
  }
  .crop-ctl .value i{font-style:normal;font-size:11px;color:var(--muted);margin-left:4px}
  .crop-hint{font-size:12px;color:var(--faint);margin-top:-3px}

  /* ---- action bar ---- */
  .actionbar{
    flex:0 0 auto;
    display:flex;justify-content:center;gap:12px;
    padding:13px 22px calc(13px + env(safe-area-inset-bottom));
    background:var(--bg);
    border-top:1px solid var(--hair);
  }
  .actionbar-inner{display:flex;gap:12px;width:100%}
  .btn{
    font-family:var(--font);font-size:15px;font-weight:600;color:var(--text);
    border-radius:var(--r-pill);border:1px solid transparent;cursor:pointer;
    padding:13px 22px;transition:transform .18s cubic-bezier(.4,0,.2,1),filter .2s,
      background .2s;
  }
  .btn:active{transform:scale(.965)}
  .btn.primary{
    flex:1 1 auto;color:#04121c;border:none;
    background:var(--accent);
    box-shadow:0 8px 20px -10px rgba(0,0,0,.8),inset 0 1px 0 rgba(255,255,255,.25);
  }
  .btn.primary:hover{filter:brightness(1.06)}
  .btn.primary[disabled]{opacity:.6;pointer-events:none}
  .btn.ghost{
    flex:0 0 auto;color:var(--text);background:var(--surface);
    border-color:var(--hair);box-shadow:var(--hi);
  }
  .btn.ghost:hover{background:rgba(255,255,255,.10)}
  .btn.ghost:active{background:rgba(255,255,255,.06)}
  .btn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(56,189,248,.40)}
  .btn.primary:focus-visible{
    box-shadow:0 8px 20px -10px rgba(0,0,0,.8),inset 0 1px 0 rgba(255,255,255,.25),
      0 0 0 3px rgba(56,189,248,.40);
  }

  /* ---- toast ---- */
  #toast{
    position:fixed;left:50%;bottom:96px;transform:translate(-50%,14px);
    padding:10px 18px;border-radius:var(--r-pill);font-size:13.5px;font-weight:560;
    color:var(--text);background:rgba(30,38,52,.96);border:1px solid var(--hair);
    box-shadow:var(--e2);
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
      border-bottom:1px solid var(--hair);scrollbar-width:none;
    }
    .side::-webkit-scrollbar{display:none}
    .navitem{width:auto;padding:9px 13px;font-size:13.5px}
    .navitem.on::after{display:none}
    .content{padding:12px 0 16px 0}
    /* the caption sits tighter once the pane is full width */
    .card-cap{padding:12px 16px 0}
    .card + .card{margin-top:14px}
    /* long-value rows stack so the value gets the full width */
    .row-wide{flex-wrap:wrap}
    .row-wide .spacer{display:none}
    .row-wide .label{white-space:nowrap}
    .row-wide input.input{flex:1 1 100%;width:100%;max-width:none}
    .row-wide .path{flex:1 1 100%;max-width:none;text-align:left;direction:ltr}
    /* the file-picker header wraps so the path gets a full line of its own */
    .row.row-col .row-head{flex-wrap:wrap;gap:8px}
    .row.row-col .row-head .spacer{display:none}
    .row.row-col .row-head .path{flex:1 1 100%;max-width:none;text-align:left;direction:ltr}
    .crop-view{max-width:none}
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
          <h1 id="hub-name">连接中…</h1>
          <p id="hub-ver">—</p>
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

  /* Two registries let a programmatic change repaint the pane in place instead
     of rebuilding it. SYNC holds one closure per control; SECT_VIS holds the
     "does this card show at all" tests. Rebuilding the DOM on every preset or
     reset (which a style swap used to do) also re-attached the crop widget's
     document listeners once per visit, leaking them one nav switch at a time. */
  var SYNC = [];
  var SECT_VIS = [];
  /* drag state for the crop preview: the document listeners are installed once
     at start-up and dispatch through here (see the DOMContentLoaded handler) */
  var CROP = {move:null, up:null};

  function whenOk(w){
    if(!w){ return true; }
    var v = String(CFG[w.key] === undefined ? '' : CFG[w.key]);
    if(w.neq && v === w.neq){ return false; }
    if(w.eq && v !== w.eq){ return false; }
    return true;
  }
  function refreshVis(){
    SECT_VIS.forEach(function(v){ v.el.hidden = !whenOk(v.when); });
  }
  function syncAll(){
    SYNC.forEach(function(f){ try{ f(); }catch(e){} });
    refreshVis();
  }

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

  /* The filled part of a slider is drawn as the input's own background-image.
     It is kept one flat colour: a gradient here was one of the last places the
     accent was being spent on decoration rather than on state. */
  function paintRange(input){
    var min = Number(input.min), max = Number(input.max), v = Number(input.value);
    var pct = max > min ? ((v - min) / (max - min)) * 100 : 0;
    input.style.backgroundImage =
      'linear-gradient(to right,#38bdf8 0%,#38bdf8 ' + pct +
      '%,rgba(255,255,255,.10) ' + pct + '%,rgba(255,255,255,.10) 100%)';
  }

  function repaintRanges(){
    var rs = document.querySelectorAll('#content input[type=range]');
    for(var i=0;i<rs.length;i++){ paintRange(rs[i]); }
  }

  /* ---- colour picker: HSV square + hue rail inside a glass popover ----
     One popover is shared by every colour row; opening it just re-points it
     at another key. Colour maths is plain HSV <-> RGB so the square, the rail
     and the hex field always agree. */
  var PICK = {el:null, key:null, on:null, h:0, s:0, v:1};

  var PRESETS = ['#0a0e16','#101522','#1c2333','#2a1f3d','#12262b','#3a1d1d',
                 '#0b2436','#241a0f','#38bdf8','#a78bfa','#22d3ee','#fbbf24'];

  function hsv2rgb(h, s, v){
    var c = v * s, k = h / 60, x = c * (1 - Math.abs(k % 2 - 1)), m = v - c;
    var r = 0, g = 0, b = 0;
    if(k < 1){ r = c; g = x; }
    else if(k < 2){ r = x; g = c; }
    else if(k < 3){ g = c; b = x; }
    else if(k < 4){ g = x; b = c; }
    else if(k < 5){ r = x; b = c; }
    else { r = c; b = x; }
    return [Math.round((r + m) * 255), Math.round((g + m) * 255),
            Math.round((b + m) * 255)];
  }
  function rgb2hex(r, g, b){
    return '#' + [r, g, b].map(function(c){
      var t = Math.max(0, Math.min(255, Math.round(c))).toString(16);
      return t.length < 2 ? '0' + t : t;
    }).join('');
  }
  function hex2rgb(s){
    var h = String(s || '').trim().replace(/^#/, '');
    if(h.length === 3){ h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2]; }
    if(!/^[0-9a-f]{6}$/i.test(h)){ return null; }
    return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16),
            parseInt(h.slice(4,6),16)];
  }
  function rgb2hsv(r, g, b){
    r /= 255; g /= 255; b /= 255;
    var mx = Math.max(r,g,b), mn = Math.min(r,g,b), d = mx - mn, h = 0;
    if(d){
      h = mx === r ? ((g - b) / d + 6) % 6 : (mx === g ? (b - r) / d + 2 : (r - g) / d + 4);
    }
    return {h:h * 60, s:mx ? d / mx : 0, v:mx};
  }
  function pickerHex(){ return rgb2hex.apply(null, hsv2rgb(PICK.h, PICK.s, PICK.v)); }

  function paintPicker(){
    var p = PICK.el;
    if(!p){ return; }
    var base = rgb2hex.apply(null, hsv2rgb(PICK.h, 1, 1));
    $('[data-sv]', p).style.background =
      'linear-gradient(to top,#000,rgba(0,0,0,0)),' +
      'linear-gradient(to right,#fff,rgba(255,255,255,0)),' + base;
    $('[data-dot]', p).style.left = (PICK.s * 100) + '%';
    $('[data-dot]', p).style.top = ((1 - PICK.v) * 100) + '%';
    $('[data-hdot]', p).style.left = (PICK.h / 360 * 100) + '%';
    var hx = $('[data-hex]', p);
    if(document.activeElement !== hx){ hx.value = pickerHex(); }
  }
  function commitPicker(){
    if(PICK.key === null){ return; }
    CFG[PICK.key] = pickerHex();
    if(PICK.on){ PICK.on(); }
  }
  function setPickerFromHex(hex){
    var rgb = hex2rgb(hex);
    if(!rgb){ return false; }
    var c = rgb2hsv(rgb[0], rgb[1], rgb[2]);
    PICK.h = c.h; PICK.s = c.s; PICK.v = c.v;
    paintPicker(); commitPicker();
    return true;
  }
  function buildPicker(){
    var p = document.createElement('div');
    p.className = 'picker';
    p.innerHTML =
      '<div class="picker-sv" data-sv><span class="picker-dot" data-dot></span></div>' +
      '<div class="picker-hue" data-hue><i data-hdot></i></div>' +
      '<input class="picker-hex" data-hex maxlength="7" spellcheck="false">' +
      '<div class="picker-presets" data-presets></div>';
    document.body.appendChild(p);
    PICK.el = p;

    function drag(e, fn){
      fn(e);
      function up(){
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.removeEventListener('touchmove', move);
        document.removeEventListener('touchend', up);
        /* a press that ends outside the popover still fires a click on body;
           remember it so that click does not close the popover mid-drag */
        PICK.dragEnd = Date.now();
      }
      function move(ev){ fn(ev); }
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      document.addEventListener('touchmove', move, {passive:false});
      document.addEventListener('touchend', up);
    }
    function svAt(e){
      var t = e.touches ? e.touches[0] : e;
      var r = $('[data-sv]', p).getBoundingClientRect();
      PICK.s = Math.min(1, Math.max(0, (t.clientX - r.left) / r.width));
      PICK.v = Math.min(1, Math.max(0, 1 - (t.clientY - r.top) / r.height));
      paintPicker(); commitPicker();
      if(e.cancelable){ e.preventDefault(); }
    }
    function hueAt(e){
      var t = e.touches ? e.touches[0] : e;
      var r = $('[data-hue]', p).getBoundingClientRect();
      PICK.h = Math.min(360, Math.max(0, (t.clientX - r.left) / r.width * 360));
      paintPicker(); commitPicker();
      if(e.cancelable){ e.preventDefault(); }
    }
    var sv = $('[data-sv]', p), hue = $('[data-hue]', p);
    sv.addEventListener('mousedown', function(e){ drag(e, svAt); });
    sv.addEventListener('touchstart', function(e){ drag(e, svAt); }, {passive:false});
    hue.addEventListener('mousedown', function(e){ drag(e, hueAt); });
    hue.addEventListener('touchstart', function(e){ drag(e, hueAt); }, {passive:false});
    $('[data-hex]', p).addEventListener('input', function(){
      setPickerFromHex($('[data-hex]', p).value);
    });
    var box = $('[data-presets]', p);
    PRESETS.forEach(function(c){
      var b = document.createElement('button');
      b.type = 'button'; b.style.background = c; b.title = c;
      b.addEventListener('click', function(){ setPickerFromHex(c); });
      box.appendChild(b);
    });
    p.addEventListener('click', function(e){ e.stopPropagation(); });
    return p;
  }
  function openPicker(key, on, anchor){
    var p = PICK.el || buildPicker();
    PICK.key = key; PICK.on = on;
    var rgb = hex2rgb(CFG[key]) || [255, 255, 255];
    var c = rgb2hsv(rgb[0], rgb[1], rgb[2]);
    PICK.h = c.h; PICK.s = c.s; PICK.v = c.v;
    paintPicker();
    p.classList.add('open');
    var r = anchor.getBoundingClientRect();
    var w = p.offsetWidth, h = p.offsetHeight;
    p.style.left = Math.min(Math.max(8, r.right - w),
                            window.innerWidth - w - 8) + 'px';
    p.style.top = Math.min(Math.max(8, r.bottom + 10),
                           window.innerHeight - h - 8) + 'px';
  }
  function closePicker(){
    if(PICK.el){ PICK.el.classList.remove('open'); }
    PICK.key = null;
  }
  document.addEventListener('click', function(){
    /* a click that lands right after a picker drag is the tail of that drag,
       not a click-away -- ignore it so the popover survives */
    if(Date.now() - (PICK.dragEnd || 0) < 400){ return; }
    closePicker();
  });

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
      SYNC.push(function(){ input.checked = !!CFG[row.key]; });
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
        input.value = Number(CFG[row.key]);
        valEl.textContent = num(input.value, d);
        paintRange(input);
      };
      input.addEventListener('input', function(){
        CFG[row.key] = Number(input.value); upd();
      });
      SYNC.push(upd);
      upd();
      return el;
    }
    if(row.type === 'enum'){
      el = document.createElement('div');
      el.className = 'row';
      el.innerHTML = '<span class="label">' + esc(row.label) + '</span>' +
        '<div class="spacer"></div><div class="seg" data-seg></div>';
      var seg = $('[data-seg]', el);
      var paint = function(){
        var cur = String(CFG[row.key] || '');
        Array.prototype.forEach.call(seg.children, function(b){
          b.classList.toggle('on', b.getAttribute('data-val') === cur);
        });
      };
      SYNC.push(paint);
      (row.options || []).forEach(function(opt){
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'seg-btn';
        b.setAttribute('data-val', opt[0]);
        b.textContent = opt[1];
        b.addEventListener('click', function(){
          CFG[row.key] = opt[0];
          /* a palette preset carries the colours it stands for, so one click
             rewrites every accent and gradient stop together */
          var preset = row.presets && row.presets[opt[0]];
          if(preset){
            Object.keys(preset).forEach(function(k){ CFG[k] = preset[k]; });
          }
          syncAll();
          /* a style swap re-bakes the whole chrome, so land it now instead of
             making the switch wait for the next save */
          applyCfg();
        });
        seg.appendChild(b);
      });
      paint();
      return el;
    }
    if(row.type === 'list'){
      el = document.createElement('div');
      el.className = 'row';
      el.innerHTML = '<span class="label">' + esc(row.label) + '</span>' +
        '<div class="spacer"></div><select class="input"></select>';
      var sel = $('select', el);
      function fill(opts){
        sel.innerHTML = '';
        opts.forEach(function(o){
          var op = document.createElement('option');
          op.value = o[0]; op.textContent = o[1];
          sel.appendChild(op);
        });
        var cur = String(CFG[row.key] || '');
        /* the stored value can be missing from the list -- the device was
           unplugged, or enumeration came back empty -- and it has to stay
           selectable instead of silently resetting to something else */
        if(cur && !opts.some(function(o){ return o[0] === cur; })){
          var op = document.createElement('option');
          op.value = cur; op.textContent = cur + '（未检测到）';
          sel.appendChild(op);
        }
        sel.value = cur;
      }
      if(row.remote){
        fill([['', '跟随系统默认']]);
        fetch(row.remote)
          .then(function(r){ return r.json(); })
          .then(function(d){
            fill([['', d.follow || '跟随系统默认']].concat(
              (d.devices || []).map(function(n){ return [n, n]; })));
          })
          .catch(function(){ toast('采集设备列表读取失败'); });
      }else{
        fill((row.options || []).map(function(o){ return [o[0], o[1]]; }));
      }
      sel.addEventListener('change', function(){
        CFG[row.key] = sel.value;
        /* a style swap re-bakes the chrome and a device swap restarts the
           capture, so neither should wait for the next save */
        applyCfg();
      });
      SYNC.push(function(){ sel.value = String(CFG[row.key] || ''); });
      return el;
    }
    if(row.type === 'color'){
      el = document.createElement('div');
      el.className = 'row';
      el.innerHTML = '<span class="label">' + esc(row.label) + '</span>' +
        '<div class="spacer"></div>' +
        '<span class="hexval" data-hexval></span>' +
        '<button class="swatch" type="button" data-sw><i></i></button>';
      var sw = $('[data-sw]', el), chip = $('i', el);
      var hexv = $('[data-hexval]', el);
      var repaint = function(){
        var c = String(CFG[row.key] || '#ffffff').toLowerCase();
        chip.style.background = c;
        hexv.textContent = c;
      };
      SYNC.push(repaint);
      sw.addEventListener('click', function(e){
        e.stopPropagation();            /* keep the document handler from closing */
        openPicker(row.key, repaint, sw);
      });
      repaint();
      return el;
    }
    if(row.type === 'file'){
      el = document.createElement('div');
      el.className = 'row row-col';
      el.innerHTML =
        '<div class="row-head">' +
          '<span class="label">' + esc(row.label) + '</span>' +
          '<div class="spacer"></div>' +
          '<span class="path" data-path></span>' +
          '<button class="btn small ghost" type="button" data-clear>清除</button>' +
          '<button class="btn small" type="button" data-pick>选择图片…</button>' +
        '</div>' +
        '<div class="crop" data-crop>' +
          '<div class="crop-view" data-view><img alt="背景取景预览" data-img></div>' +
          '<div class="crop-ctl">' +
            '<input type="range" min="1" max="4" step="0.05" data-zoom>' +
            '<span class="value"><em data-zval>1.00</em><i>×</i></span>' +
            '<button class="btn small ghost" type="button" data-reset>重置取景</button>' +
          '</div>' +
          '<div class="crop-hint">拖动图片平移取景，滑条缩放；这里的画面就是屏幕上的效果</div>' +
        '</div>';
      var pathEl = $('[data-path]', el);
      var pickBtn = $('[data-pick]', el);
      var clearBtn = $('[data-clear]', el);
      var cropEl = $('[data-crop]', el);
      var view = $('[data-view]', el);
      var imgEl = $('[data-img]', el);
      var zoomEl = $('[data-zoom]', el);
      var zvalEl = $('[data-zval]', el);

      /* metres of source per pixel of preview: mirrors the renderer's framing
         (cover-fit, then zoom about the centre and slide a 320x170 window) */
      function geom(){
        var cw = view.clientWidth, ch = view.clientHeight;
        var iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
        if(!cw || !ch || !iw || !ih){ return null; }
        var zoom = Math.min(4, Math.max(1, Number(CFG.bg_image_zoom) || 1));
        var cover = Math.max(cw / iw, ch / ih) * zoom;
        return {cw:cw, ch:ch, iw:iw, ih:ih, dw:iw*cover, dh:ih*cover};
      }
      function frame(){
        var g = geom();
        if(!g){ return; }
        imgEl.style.width = g.dw + 'px';
        imgEl.style.height = g.dh + 'px';
        var maxx = Math.max(0, g.dw - g.cw), maxy = Math.max(0, g.dh - g.ch);
        var ox = Math.min(1, Math.max(-1, Number(CFG.bg_image_offset_x) || 0));
        var oy = Math.min(1, Math.max(-1, Number(CFG.bg_image_offset_y) || 0));
        imgEl.style.transform = 'translate(' + (-(1+ox)*maxx/2) + 'px,' +
                                             (-(1+oy)*maxy/2) + 'px)';
      }
      function show(){
        var p = CFG[row.key] || '';
        pathEl.textContent = p || '未选择 · 使用上方自定渐变';
        pathEl.title = p;
        pathEl.classList.toggle('empty', !p);
        clearBtn.disabled = !p;
        cropEl.style.display = p ? '' : 'none';
        if(p){ imgEl.src = '/api/bg-image?t=' + Date.now(); }
        var z = Math.min(4, Math.max(1, Number(CFG.bg_image_zoom) || 1));
        zoomEl.value = z;
        zvalEl.textContent = z.toFixed(2);
        paintRange(zoomEl);
        frame();
      }
      imgEl.addEventListener('load', frame);
      zoomEl.addEventListener('input', function(){
        CFG.bg_image_zoom = Number(zoomEl.value);
        zvalEl.textContent = Number(zoomEl.value).toFixed(2);
        paintRange(zoomEl); frame();
      });
      $('[data-reset]', el).addEventListener('click', function(){
        CFG.bg_image_zoom = 1; CFG.bg_image_offset_x = 0; CFG.bg_image_offset_y = 0;
        zoomEl.value = 1; zvalEl.textContent = '1.00'; paintRange(zoomEl); frame();
        toast('取景已重置');
      });
      var drag = null;
      function onDown(e){
        var t = e.touches ? e.touches[0] : e;
        drag = {x:t.clientX, y:t.clientY,
                ox:Number(CFG.bg_image_offset_x) || 0,
                oy:Number(CFG.bg_image_offset_y) || 0};
        if(e.cancelable){ e.preventDefault(); }
      }
      function onMove(e){
        if(!drag){ return; }
        var g = geom();
        if(!g){ return; }
        var t = e.touches ? e.touches[0] : e;
        var maxx = Math.max(1, g.dw - g.cw), maxy = Math.max(1, g.dh - g.ch);
        CFG.bg_image_offset_x =
          Math.min(1, Math.max(-1, drag.ox - 2*(t.clientX - drag.x)/maxx));
        CFG.bg_image_offset_y =
          Math.min(1, Math.max(-1, drag.oy - 2*(t.clientY - drag.y)/maxy));
        frame();
        if(e.cancelable){ e.preventDefault(); }
      }
      function onUp(){ drag = null; }
      view.addEventListener('mousedown', onDown);
      view.addEventListener('touchstart', onDown, {passive:false});
      /* the document-level half of the drag is installed once at start-up and
         dispatched through CROP, so revisiting this page cannot pile up copies */
      CROP.move = onMove;
      CROP.up = onUp;
      SYNC.push(show);

      clearBtn.addEventListener('click', function(){
        CFG[row.key] = ''; show(); toast('已清除，保存后生效');
      });
      pickBtn.addEventListener('click', function(){
        pickBtn.disabled = true; pickBtn.textContent = '选择中…';
        fetch('/api/pick-image', {method:'POST'})
          .then(function(r){ return r.json(); })
          .then(function(d){
            if(d && d.ok && d.path){
              CFG[row.key] = d.path;
              /* the crop preview is streamed from the server's config, so the
                 pick has to be applied before the new file can be framed --
                 without this the preview keeps serving the previous image (or
                 a 404) and the widget only works after a manual save + reload */
              applyCfg().then(show, show);
              toast('已选择并应用');
            } else if(d && d.cancelled){
              toast('已取消');
            } else {
              toast('选择失败，可手动填写路径');
            }
          })
          .catch(function(){ toast('选择失败，可手动填写路径'); })
          .then(function(){ pickBtn.disabled = false; pickBtn.textContent = '选择图片…'; });
      });
      show();
      window.addEventListener('resize', frame);
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
    SYNC.push(function(){ input.value = CFG[row.key]; });
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
    SYNC.length = 0;
    SECT_VIS.length = 0;
    var g = SCHEMA[active];
    if(!g){ return; }
    var head = document.createElement('div');
    head.className = 'content-title anim';
    head.innerHTML = (g.icon || '') + '<span>' + esc(g.title) + '</span>';
    root.appendChild(head);
    (g.sections || []).forEach(function(sec){
      var card = document.createElement('div');
      card.className = 'card anim';
      if(sec.caption){
        var cap = document.createElement('div');
        cap.className = 'card-cap';
        cap.textContent = sec.caption;
        card.appendChild(cap);
      }
      sec.rows.forEach(function(row){ card.appendChild(buildRow(row)); });
      root.appendChild(card);
      if(sec.when){ SECT_VIS.push({el: card, when: sec.when}); }
    });
    /* each row has already painted itself; only the conditional cards need a
       first pass, and syncAll would re-run every row (including a fresh
       backdrop fetch) for nothing */
    refreshVis();
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
    /* the header names the hardware, not the app: the hub reports its own
       vendor and firmware version off the USB descriptors */
    var hubName = $('#hub-name'), hubVer = $('#hub-ver'), hub = stats.hub || {};
    if(hub.product || hub.vendor){
      hubName.textContent = hub.product || hub.vendor;
      var bits = [];
      if(hub.vendor && hub.vendor !== (hub.product || '')){ bits.push(hub.vendor); }
      if(hub.version){ bits.push('v' + hub.version); }
      hubVer.textContent = bits.join(' · ') || '—';
    } else {
      hubName.textContent = stats.connected ? '未识别设备' : '未连接';
      hubVer.textContent = '—';
    }
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

  /* Push the whole panel state to the server. The render loop reads the server
     config live, so this is both "make it take effect on the screen" and what
     lets /api/bg-image serve a file the panel has just chosen. */
  function applyCfg(){
    return fetch('/api/config', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(CFG)
    }).then(function(r){ return r.json(); })
      .then(function(d){
        if(d && d.ok && d.config){
          CFG = d.config;
          /* the server echoes back what it stored, so re-read it into the
             controls: anything it coerced or rejected shows up here */
          if(SYNC.length){ syncAll(); }
        }
        return !!(d && d.ok);
      });
  }

  function save(){
    var btn = $('#save');
    btn.disabled = true;
    var label = btn.textContent;
    applyCfg().then(function(ok){
      if(ok){
        btn.textContent = '已保存';
        toast('设置已保存');
        setTimeout(function(){ btn.textContent = label; }, 1200);
      } else {
        toast('保存失败');
      }
    }).catch(function(){ toast('保存失败'); })
      .then(function(){ btn.disabled = false; });
  }

  function reset(){
    CFG = JSON.parse(JSON.stringify(DEFAULTS));
    /* synced in place: a rebuild would drop the scroll position and re-attach
       the crop widget's listeners all over again */
    syncAll();
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
    /* the crop preview's drag listeners live here, once, rather than being
       re-attached every time the background page is rebuilt */
    document.addEventListener('mousemove', function(e){
      if(CROP.move){ CROP.move(e); }
    });
    document.addEventListener('mouseup', function(){ if(CROP.up){ CROP.up(); } });
    document.addEventListener('touchmove', function(e){
      if(CROP.move){ CROP.move(e); }
    }, {passive:false});
    document.addEventListener('touchend', function(){ if(CROP.up){ CROP.up(); } });
    /* the popover is anchored to the viewport, so a scroll would leave it
       floating away from its swatch -- close it instead */
    $('#content').addEventListener('scroll', closePicker);
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
    schema = [{"title": g["title"], "icon": g["icon"], "sections": g["sections"]}
              for g in SCHEMA]
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
    choose_image = None
    device_list = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "hubmon"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # only ever serves the file the user picked, so the panel can show it
    _IMG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".bmp": "image/bmp", ".gif": "image/gif", ".webp": "image/webp"}

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

    def _serve_bg_image(self):
        """Stream the configured backdrop image so the crop preview can use it."""
        p = (self.server.cfg.bg_image or "").strip().strip('"')
        ctype = self._IMG_TYPES.get(os.path.splitext(p)[1].lower())
        if not p or not ctype or not os.path.exists(p):
            self._json(404, {"ok": False, "error": "no image"})
            return
        try:
            with open(p, "rb") as f:
                body = f.read()
        except OSError as exc:
            self._json(404, {"ok": False, "error": str(exc)})
            return
        self._respond(200, body, ctype)

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/":
                html = _render_page(self.server.cfg).encode("utf-8")
                self._respond(200, html, "text/html; charset=utf-8")
            elif path == "/icon.svg":
                self._respond(200, _ICON_APP.encode("utf-8"), "image/svg+xml")
            elif path == "/api/config":
                self._json(200, self.server.cfg.to_dict())
            elif path == "/api/bg-image":
                self._serve_bg_image()
            elif path == "/api/stats":
                data = self.server.provider()
                self._json(200, data if isinstance(data, dict) else {})
            elif path == "/api/spectrum-devices":
                # the list comes from an injected callable: this handler runs on
                # a thread of its own, and bringing up the capture library's COM
                # apartment is the provider's business, not the panel's
                names = []
                if self.server.device_list is not None:
                    try:
                        names = list(self.server.device_list())
                    except Exception:
                        names = []
                self._json(200, {"follow": "跟随系统默认", "devices": names})
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
            elif path == "/api/pick-image":
                fn = self.server.choose_image or pick_image_path
                picked = fn()
                if picked:
                    self._json(200, {"ok": True, "path": picked})
                else:
                    self._json(200, {"ok": False, "cancelled": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def start(cfg, provider, port=None, open_browser=False, on_quit=None,
          choose_image=None, device_list=None):
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
    httpd.choose_image = choose_image or pick_image_path
    httpd.device_list = device_list

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