# 音频频谱模式 实现计划

> **给执行者：** 使用 `03-subagent-development` skill（推荐）或 `04-plan-execution` skill 逐任务实现。
> **目标：** 给 hubmon 增加「音频频谱模式」——采集默认播放设备的回环音频，用 24 段对数频谱以三种风格全屏呈现，并顺带把 CPU/GPU 卡片内的瓦数做成可开关。
> **架构：** 新增 `hubmon/audio.py` 承担采集 + 分析（独立线程、环形缓冲、惰性依赖、逐级降级）；`hubmon/render.py` 增加频谱的静态烘焙与三种绘制分派，与既有「静态底图 + 逐帧叠加」的架构一致；`hubmon/main.py` 负责把分析结果喂给渲染层并按 `mode` 启停采集线程。
> **技术栈：** Python 3.11 · numpy（FFT）· Pillow（绘制）· soundcard（WASAPI 回环采集）· PyInstaller（打包）
> **设计依据：** `docs/superpowers/specs/2026-10-14-audio-spectrum-mode-design.md`

**全局验收（每个任务完成后都要保持成立）**
- `python -m compileall -q hubmon` 通过
- 仪表盘模式（`mode="dash"`）的画面对本次改动**零回归**：`style_link.py` 每帧脏区不高于 glass 15.0 KiB / quartz 16.7 KiB，`style_cost.py` 两面 < 16.7 ms/帧

---

## 任务 1: 采集能力 spike（先证伪，再动手）

**文件：** 创建 `build/_pool/spectrum_spike.py`

- [ ] **步骤 1: 写探测脚本**

```python
"""Does sounddevice actually give us WASAPI loopback on this machine?"""
import sounddevice as sd
print("sounddevice", sd.__version__)
print("devices:", [d["name"] for d in sd.query_devices()])
try:
    s = sd.WasapiSettings(loopback=True)
    print("WasapiSettings(loopback=True) accepted")
except TypeError as e:
    print("NO LOOPBACK SUPPORT:", e)
```
- [ ] **步骤 2: 运行，记录结论**

```
python -m pip install sounddevice
python build\_pool\spectrum_spike.py
```
**通过条件**：`WasapiSettings(loopback=True)` 被接受，且 `query_devices()` 里能看到至少一个输出设备名。

- [x] **步骤 3: 若失败的处理（不是占位符，是明确的二选一）**
  - **已发生并已完成**：`sounddevice` 0.5.6 的 `WasapiSettings` 不接受 `loopback` → 改用 `soundcard`，实测通过（枚举到 9 个回环端点，默认扬声器 `SteelSeries Sonar - Gaming`，录到 9600×2 帧真实数据，rms 0.0013）。`audio.py` 对外接口不变，只换内部实现；`requirements.txt` / `build.py` 同步改为 `soundcard`（`sounddevice` 已卸载）。
  - 若两者都拿不到回环 → 停下来向我报告，不要继续后续任务。
- [ ] **步骤 4: 提交**

```
Add a loopback capability spike for the spectrum mode
```

---

## 任务 2: 分析层 `hubmon/audio.py`

**文件：** 创建 `hubmon/audio.py`；创建测试 `build/_pool/spec_analyser.py`

对外接口（后续任务只依赖这四项）：
```python
BANDS = 24
class Spectrum:
    def __init__(self, device: str = ""): ...
    def start(self) -> None: ...            # 幂等；任何失败都只是把 state 改掉
    def stop(self) -> None: ...             # 幂等
    def levels(self, dt: float) -> tuple[list[float], list[float]]: ...
    state: str                              # "ok" / "no-lib" / "no-device" / "error"
```

- [ ] **步骤 1: 写失败测试**

```python
"""Analyser maths, driven by synthetic blocks -- no audio hardware needed."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from hubmon.audio import Spectrum, BANDS

def feed(spec, sig, dt):
    spec._latest = sig                     # inject a block the way the thread would
    return spec.levels(dt)

def sine(f, amp):
    return (amp * np.sin(2 * np.pi * f * np.arange(1024) / 48000)).astype("float32")

s = Spectrum()
b, p = feed(s, sine(1000.0, 0.5), 1 / 30)
peak_band = int(np.argmax(b))
assert b[peak_band] > 0.6, b
assert peak_band == 12 or peak_band == 13, peak_band     # 1 kHz lives in band 12/13
assert abs(sum(b[:3]) / 3) < 0.05, "bass must stay quiet for a 1 kHz tone"

# attack: full scale within ~0.05 s
for _ in range(4):
    b, _ = feed(s, sine(1000.0, 1.0), 1 / 30)
assert b[peak_band] > 0.9, b[peak_band]

# release: falls back after ~0.25 s of silence
for _ in range(2):
    b, _ = feed(s, np.zeros(1024, "float32"), 1 / 30)
assert b[peak_band] < 0.9 and b[peak_band] > 0.2, b[peak_band]

# peak hold outlives the band, then falls
assert p[peak_band] >= b[peak_band]
print("analyser OK")
```
- [ ] **步骤 2: 运行确认失败**（`ModuleNotFoundError: hubmon.audio`）
- [ ] **步骤 3: 写最小实现**

要点（全部按设计文档 §2）：
```python
SR, BLOCK, FFT_N = 48000, 1024, 1024
FMIN, FMAX, FLOOR_DB = 40.0, 16000.0, -60.0
ATTACK, RELEASE, PEAK_HOLD = 0.05, 0.25, 0.8
_EDGES = np.geomspace(FMIN, FMAX, BANDS + 1)
_BINS = np.fft.rfftfreq(FFT_N, 1.0 / SR)
_WINDOW = np.hanning(FFT_N)
```
- `_analyse(block)` → 24 段值：`|rfft(block * _WINDOW)|` → 每段取 `_BINS` 落在 `_EDGES[i].._EDGES[i+1]` 的**峰值** → `dB = 20*log10(max(mag, eps))` → `(dB - FLOOR_DB) / -FLOOR_DB` 截到 0..1。
- `levels(dt)`：按 `dt` 步进包络（`a = 1 - exp(-dt/tau)`；上升用 ATTACK、下降用 RELEASE）；对每段维护 `_hold`/`_hold_at`，**0.8s 内不落**，之后按 RELEASE 同速下落。
- 采集线程：在 `sc.get_microphone(...).recorder(samplerate=SR, channels=2)` 上下文里循环 `rec.record(numframes=BLOCK)`，把每块拷进 `self._latest`；`start()` 用 `try/except` 包住，异常只改 `state`，**不抛**。
- 惰性导入：`import soundcard as sc` 放在 `try` 里，失败 → `state = "no-lib"`，`levels()` 返回全 0。

- [ ] **步骤 4: 运行确认通过**（`python build\_pool\spec_analyser.py` → `analyser OK`）
- [ ] **步骤 5: 提交**

```
Add the 24-band log analyser: loopback capture, envelope, peak hold
```

---

## 任务 3: 配置项与面板

**文件：** 修改 `hubmon/config.py`、`hubmon/webui.py`；创建测试 `build/_pool/panel_spec.py`

- [ ] **步骤 1: 写失败测试** —— 断言四个新字段存在、默认值正确、SCHEMA 里能查到对应行、`update_from()` 能接收并落盘：
```python
from hubmon import config, webui
c = config.Config()
assert (c.mode, c.spectrum_style, c.spectrum_device, c.show_watt) == ("dash", "led", "", True)
keys = {r["key"] for g in webui.SCHEMA for s in g["sections"] for r in s["rows"]}
assert {"mode", "spectrum_style", "spectrum_device", "show_watt"} <= keys
```
- [ ] **步骤 2: 运行确认失败**
- [ ] **步骤 3: 写最小实现**
  - `config.py`：`mode: str = "dash"` / `spectrum_style: str = "led"` / `spectrum_device: str = ""` / `show_watt: bool = True`（放在 `# ---- look ----` 段）。
  - `webui.py` SCHEMA：「画面」组新增子区 `{"caption": "显示模式", "rows": [...]}`（`mode` 用 `enum`：`dash`=仪表盘 / `spectrum`=音频频谱；`spectrum_style` 用 `enum`：`led` / `peak` / `mirror`；`spectrum_device` 用 `text`）；「显示调节」子区加一行 `{"key": "show_watt", "label": "卡片内瓦数", "type": "bool"}`。
- [ ] **步骤 4: 运行确认通过**
- [ ] **步骤 5: 提交**

```
Expose the spectrum mode, its style, its device and the in-card watt toggle
```

---

## 任务 4: 频谱的静态烘焙

**文件：** 修改 `hubmon/render.py`；创建测试 `build/_pool/spec_bake.py`

- [ ] **步骤 1: 写失败测试** —— 进频谱模式后烘焙一次，断言：幽灵格存在但极暗、基线导轨在 `y=152`、刻度标签落在 `x=48..300` 且不压到表壳螺钉、dB 网格线存在：
```python
cfg = Config(); cfg.mode = "spectrum"; cfg.style = "quartz"
d = Dashboard(cfg); d.update(Demo().snapshot()); img = d.render(1/30)
px = img.load()
assert px[60, 152][0] < 60                      # baseline rail is faint, not white
assert any(px[x, 158] != px[x, 140] for x in range(48, 300))   # tick labels drew
for cx in (9, 311):                             # case screws must stay clear
    assert px[cx, 158] == px[cx, 20]
```
- [ ] **步骤 2: 运行确认失败**
- [ ] **步骤 3: 写最小实现**
  - 模块级几何常量（与设计文档 §3 一致）：`SPEC_X0, SPEC_X1 = 8, 312`、`SPEC_TOP, SPEC_BOT = 33, 153`、`SPEC_ROW, SPEC_ROWS = 10, 12`、`SPEC_MID = 93`、`SPEC_TICK_Y = 154`、`SPEC_RAIL_Y = 152`、`BANDS = 24`。
  - `_build_static()` 增加分支：`if c.mode == "spectrum":` → 底图（quartz 走 `_dial()`）→ `_spectrum_chrome(img, c)` 取代卡片循环；顶栏走既有 `_plate()`（quartz）。
  - `_spectrum_chrome()` 烘焙：① 幽灵格（13% 白）、基线导轨、三个刻度（100/1k/10k，`x=48..300`）、④ 的 dB 网格线、⑤ 的中缝发丝线。全部一次画完。
- [ ] **步骤 4: 运行确认通过**
- [ ] **步骤 5: 提交**

```
Bake the spectrum chrome: ghost cells, rails, decade ticks, dB grid
```

---

## 任务 5-7: 三种风格（每任务一组，结构相同）

三个任务分别实现 ① LED 点阵柱 / ④ 峰值保持 / ⑤ 镜像渐变柱。**任务 8 之前 `mode="spectrum"` 只画 chrome，不画柱子**，因此三者互不阻塞。

### 任务 5: ① LED 点阵柱
**文件：** 修改 `hubmon/render.py`；创建测试 `build/_pool/spec_led.py`

- [ ] **步骤 1: 写失败测试** —— 断言：点亮格实心（不透明）、最高格更亮、灭格 ≤ 20/255、柱高随电平单调：
```python
cfg = Config(); cfg.mode = "spectrum"; cfg.spectrum_style = "led"
d = Dashboard(cfg); d.spectrum_bands = [0.9] * 24; d.spectrum_peaks = [0.9] * 24
img = d.render(1/30); px = img.load()
col = [px[20, y][2] for y in range(33, 153)]        # blue channel down band 0
lit = [y for y in range(33, 153) if col[y - 33] > 120]
assert len(lit) >= 100                               # 0.9 of 12 rows
assert max(col) - min([c for c in col if c > 120]) > 15   # top cell brighter
assert max(px[20, y][0] for y in range(33, 43)) < 20      # unlit ghost is dark
```
- [ ] **步骤 2-4: 失败 → 实现 → 通过**
  - 实现要点：每段 10px 方格、灭格用 `(255,255,255,13)`、亮格实心 accent、最高一格 `mix(accent, white, 0.42)`；柱高 `round(level*12)` 格。
- [ ] **步骤 5: 提交**

```
Draw style 1: the LED matrix spectrum
```

### 任务 6: ④ 峰值保持
- [ ] **步骤 1: 写失败测试** —— 柱体为平色（同一段内颜色一致）、柱高量化到 2px、帽在柱顶之上、`peak > 0.88` 的段帽为琥珀：
```python
cfg.spectrum_style = "peak"; d.spectrum_peaks = [0.95] + [0.2] * 23
img = d.render(1/30); px = img.load()
ys = [y for y in range(33, 153) if px[12, y][2] > 80]
assert (SPEC_BOT - min(ys)) % 2 == 0                      # quantised
cap = [y for y in range(33, 153) if px[12, y][0] > 180 and px[12, y][2] < 170]
assert cap and cap[0] < min(ys)                            # amber cap sits above the bar
```
- [ ] **步骤 2-4:** 实现要点：柱体 `mix(accent, GLASS, 0.34)` 实心、高 `int(level*120)//2*2`；帽 3px、颜色 `ACCENT_AMBER if peak > 0.88 else accent`。
- [ ] **步骤 5: 提交**

```
Draw style 4: peak-hold bars with an amber near-full cap
```

### 任务 7: ⑤ 镜像渐变柱
- [ ] **步骤 1: 写失败测试** —— 断言上下镜像对称、外端比中心亮、渐变条按高度缓存命中：
```python
cfg.spectrum_style = "mirror"
img = d.render(1/30); px = img.load()
up = [px[20, y][2] for y in range(33, 93)]
dn = [px[20, y][2] for y in range(93, 153)]
assert abs(sum(up) - sum(dn)) / max(1, sum(up)) < 0.15     # mirrored
assert up[0] > up[-1] + 20                                  # bright outside, dim centre
assert len(Dashboard._grad_cache) <= 61                     # one strip per 2px step
```
- [ ] **步骤 2-4:** 实现要点：以 `SPEC_MID=93` 为轴；柱高 `int(level*60)//2*2`；径向（竖直）渐变条**按量化高度预烘焙缓存**（类级 dict，键＝高度）；外端用遮罩做圆头。
- [ ] **步骤 5: 提交**

```
Draw style 5: mirrored gradient bars off a centre hairline
```

---

## 任务 8: 模式接线（采集线程 + 渲染分派）

**文件：** 修改 `hubmon/main.py`、`hubmon/render.py`；创建测试 `build/_pool/spec_wire.py`

- [ ] **步骤 1: 写失败测试** —— 断言仪表盘模式下瓦数仍显示、频谱模式下走频谱分支且 `_look_sig` 随 mode/style 变化：
```python
from hubmon.render import Dashboard
sig = lambda m, s: Dashboard(_cfg(m, s))._look_sig()
assert sig("dash", "led") != sig("spectrum", "led")     # mode must trigger a re-bake
assert sig("spectrum", "led") != sig("spectrum", "mirror")
```
- [ ] **步骤 2: 运行确认失败**（`_look_sig` 与 mode 无关）
- [ ] **步骤 3: 写最小实现**
  - `render.py`：`_look_sig()` 加入 `c.mode, c.spectrum_style`；`render()` 在 `mode == "spectrum"` 时跳过卡片与标签、只画频谱 + 顶栏。
  - `main.py`：`run_stream` 里 `spec = audio.Spectrum(cfg.spectrum_device)`；每轮
    ```python
    want = (cfg.mode == "spectrum")
    spec.start() if want else spec.stop()
    bands, peaks = spec.levels(dt) if want else ([0.0]*24, [0.0]*24)
    stats = sensor.snapshot()
    stats["spectrum_bands"], stats["spectrum_peaks"] = bands, peaks
    dash.update(stats)
    ```
  - `Demo.snapshot()` 增加合成的 `spectrum_bands`（例如随 t 移动的驻波）以便 `--demo` 预览。
  - 面板 `provider()` 增加 `spec_state`，让面板能显示采集状态。
- [ ] **步骤 4: 运行确认通过** —— `python -m hubmon.main --demo --preview build\_pool\spec_preview`，肉眼核对出图
- [ ] **步骤 5: 提交**

```
Wire the spectrum mode into the render loop and the panel
```

---

## 任务 9: 瓦数开关

**文件：** 修改 `hubmon/render.py`；创建测试 `build/_pool/spec_watt.py`

- [ ] **步骤 1: 写失败测试** —— `show_watt=False` 时 CPU/GPU 卡内 `~36 W` 消失，**功率卡 PWR·EST 不受影响**：
```python
cfg = Config(); cfg.show_watt = False
img = Dashboard(cfg).render(1/30)
# CPU card's watt readout sits at (g.x+g.w-PAD-w, y=58); assert that band is backdrop
assert not _has_ink(img, (200, 58, 311, 70))            # gpu watt gone
assert _has_ink(img, (20, 123, 106, 140))               # PWR card still there
```
- [ ] **步骤 2-4:** 实现要点：`_big_card()` 的 `rhs` 传 `f"~{...} W" if c.show_watt else ""`，并跳过空串绘制；**不进 `_look_sig()`**（动态文本）。
- [ ] **步骤 5: 提交**

```
Make the in-card watt readout switchable without touching the power card
```

---

## 任务 10: 打包

**文件：** 修改 `requirements.txt`、`build.py`

- [ ] **步骤 1:** `requirements.txt` 加 `soundcard>=0.4.6`（连同 cffi / pycparser）
- [ ] **步骤 2:** `build.py` 的参数列表加 `--collect-all soundcard`（与既有 `--collect-all libusb_package` 并列；cffi / pycparser 由 soundcard 的依赖链带入）
- [ ] **步骤 3: 验证打包后能枚举回环设备** —— **必须先结束正在运行的 `hubmon.exe`**（否则 PyInstaller 删旧 exe 报 `WinError 5`）：
```
Get-Process hubmon | Stop-Process -Force
python build.py
.\dist\hubmon.exe --no-browser     # 在面板里切到频谱模式，确认有读数而非静默
```
- [ ] **步骤 4: 提交**

```
Bundle sounddevice so the packaged build can capture loopback audio
```

---

## 任务 11: 带宽与实机验证

**文件：** 修改 `build/_pool/style_cost.py`、`build/_pool/style_link.py`（扩展为可扫 `mode`），创建 `build/_pool/spec_fallback.py`

- [ ] **步骤 1:** 扩展两个度量脚本：遍历 `(face, mode, spectrum_style)` 组合
- [ ] **步骤 2: 跑度量** —— 目标：频谱模式 30fps 下每帧脏区 **≤ 300 KiB/s**（即 ≤ 10 KiB/帧 @30fps），渲染耗时 < 16.7 ms/帧
- [ ] **步骤 3: 跑降级测试** `spec_fallback.py`：把 `sys.modules["sounddevice"] = None` 后再构造 `Spectrum`，断言 `state == "no-lib"`、`levels()` 返回全 0、**不抛异常**
- [ ] **步骤 4: 实机 60s** —— 启动真实 exe，切到频谱模式播放音乐，读日志里的 fps 与 KiB/s：
  - 通过条件：帧率 ≥ 45 fps，KiB/s ≤ 300
  - 6 组全测：3 风格 × 2 壳体
- [ ] **步骤 5: 提交**

```
Measure and record the spectrum mode's bandwidth and frame cost
```

---

## 任务 12: 收尾

- [ ] **步骤 1:** 删除临时脚本 `build/_pool/spectrum_spike.py`、`spectrum_sketch.py`、各 `spec_*.py` 与出图；保留可复用的 `style_cost.py` / `style_link.py`
- [ ] **步骤 2:** 复核全局验收（compileall + 仪表盘模式零回归）
- [ ] **步骤 3:** 提交并推送（**推送用 `socks5h://127.0.0.1:7897`，该代理对 `github.com` 有概率性 TLS 重置，失败重试 1–2 次即可**）
- [ ] **步骤 4:** 报告实测数据（fps / KiB/s / 每帧脏区 / 三种风格各自的开销）

---

## 自查

- **Spec 覆盖**：设计文档 §1 数据层 → 任务 1/2/8；§2 频域层 → 任务 2；§3 渲染层 → 任务 4-7；§4 模式与配置 → 任务 3/8/9；§5 带宽与验证 → 任务 11。§6「明确不做」未出现在任何任务里。✅
- **占位符**：无 TBD / TODO；任务 1 的失败分支给了具体的替代库，不是占位符。✅
- **类型一致性**：`Spectrum.levels(dt)` 全程返回 `(list[float], list[float])`，长度均为 `BANDS=24`；`Dashboard.spectrum_bands` / `spectrum_peaks` 与之一致。✅
- **风险**：任务 5-7 的三份测试都直接读像素，`SPEC_*` 常量改名时会一起失效 —— 测试里直接 import 这些常量，避免硬编码坐标漂移。

---

## 执行交接

请选执行方式（我会照此开工）：
1. **子代理驱动** —— `03-subagent-development`：任务 5/6/7 三条风格互不阻塞，可并行派发。
2. **当前会话内联执行** —— `04-plan-execution`：我在本会话按任务顺序做，每个任务后跑验收。
