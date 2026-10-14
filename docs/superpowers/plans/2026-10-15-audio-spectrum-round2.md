# 音频频谱模式 · 第二轮迭代 实现计划
> **给执行者：** 使用 `04-plan-execution` skill 在当前会话逐任务实现（改动集中在 `render.py` 一个文件，串行更安全）。
**目标：** 把频谱从 3 种风格扩到 8 种、把频段利用率拉平、把抖动压下去，并把采集设备改成面板选取器。
**架构：** 分析层加每段倾斜增益与可调包络；渲染层把风格分发从 `if/elif` 改成两张注册表（静态 chrome / 逐帧 bars），新增风格只加两个函数；面板新增 `list` 行类型 + 动态设备接口。
**技术栈：** Python 3.11 · numpy · Pillow · soundcard(WASAPI 回环，注意 COM 公寓按线程) · stdlib HTTP 面板 · 断言脚本在 `build/_pool/`（无正式测试框架，沿用第一轮）。

**基准数字（第一轮实测，本轮所有门限都对着它比）：** LED 12.3 KiB/帧（30fps 折算 368 KiB/s）、peak 21.9 KiB/帧（658 KiB/s）、mirror 33.6 KiB/帧（1008 KiB/s，因此被删）。渲染耗时三种都在 2.6–4.3 ms/帧。

---
```

### 任务 1: 每段倾斜增益 + 包络放缓
**文件：** 修改 `hubmon/audio.py`；测试 `build/_pool/spec_tilt.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 合成一段**粉红噪声**（各八度等能量），断言倾斜后各段电平的极差收窄；再断言 1kHz 处的增益为 0dB：
```python
# 24 段各喂一个该段中心频率的正弦（幅度按 1/sqrt(f) 递减，模拟粉红）
# 期望：加了 TILT 之后 max(levels) - min(levels) < 0.35（不加时 > 0.6）
# 期望：fc 最接近 1kHz 那一段的 gain_db 绝对值 < 0.5
```
- [ ] **步骤2: 运行确认失败** —— `python build\_pool\spec_tilt.py` 报极差过大。
- [ ] **步骤3: 编写最小实现** —— `audio.py` 新增 `TILT_DB_PER_OCT = 3.0`、`TILT_REF_HZ = 1000.0`、模块级 `_TILT_DB = TILT_DB_PER_OCT * np.log2(fc / TILT_REF_HZ)`（`fc` 由 `_EDGES` 的几何中心算出），`_analyse()` 里在 `20*log10(peak)` 之后加 `db += _TILT_DB[i]`；`ATTACK, RELEASE = 0.09, 0.38`。
- [ ] **步骤4: 运行确认通过** —— `python build\_pool\spec_tilt.py` 与 `python build\_pool\spec_analyser.py` 同时绿（**后者与本步骤的常数绑定，必须一起改**：断言 4 帧后 level 的手算值随 ATTACK 变了）。
- [ ] **步骤5: 提交** —— `git commit -m "Tilt the analyser's bands and slow its envelope"`

### 任务 2: 两个滑条（config + 面板）
**文件：** 修改 `hubmon/config.py`、`hubmon/webui.py`；测试 `build/_pool/panel_spec.py`（扩展）
- [ ] **步骤1: 编写失败测试** —— 断言新字段默认值（`spectrum_smoothing=0.5`、`spectrum_tilt=3.0`）、SCHEMA 里有这两行、且 `_look_sig()` **不**随它们变化（动态参数，不该触发重烘焙）：
```python
c1, c2 = Config(), Config(); c2.spectrum_smoothing, c2.spectrum_tilt = 0.9, -2.0
assert Dashboard(c1)._look_sig() == Dashboard(c2)._look_sig()
```
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— `Config` 加两个字段；SCHEMA「音频频谱」子区加两行 `{"type": "num", "min": 0, "max": 1, "step": 0.05}`（smoothing）与 `{"type": "num", "min": -6, "max": 6, "step": 0.5, "unit": "dB/oct"}`（tilt）；`audio.Spectrum.levels()` 接收这两个值（构造时传入，`main.py` 里随 `cfg` 热更新 —— 与 `spectrum_device` 同一条路径）。
- [ ] **步骤4: 运行确认通过** —— `panel_spec.py` 绿；`python -m compileall -q hubmon` 绿。
- [ ] **步骤5: 提交**

### 任务 3: `list` 行类型 + 动态设备接口
**文件：** 修改 `hubmon/webui.py`；测试 `build/_pool/panel_list.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 起一个临时端口的真面板，`GET /api/spectrum-devices`，断言返回 `{"follow": ..., "devices": [...]}` 且**没有抛异常**（COM 未初始化会在这里炸）；再断言 SCHEMA 的 `spectrum_style`/`spectrum_device` 两行是 `list` 类型、`options` 由接口提供。
```python
srv = webui.start(cfg, provider, open_browser=False, port=8799)
body = json.loads(urlopen("http://127.0.0.1:8799/api/spectrum-devices").read())
assert body["follow"] and isinstance(body["devices"], list)
```
- [ ] **步骤2: 运行确认失败** —— 404（接口还不存在）。
- [ ] **步骤3: 编写最小实现** —— `webui.do_GET` 加分支 `elif path == "/api/spectrum-devices": self._json(200, spectrum_devices())`；`spectrum_devices()` 里 **先 `audio._com_apartment()`**，再 `soundcard.all_speakers()` 取名，任何异常都返回空列表（不报错）；JS 的 `buildRow` 加 `row.type === 'list'` 分支：`<select>`，选项来自 `row.remote` 指定的接口（`fetch` 一次，缓存），把当前 `CFG[key]` 作为附加项注入当它不在列表里时。
- [ ] **步骤4: 运行确认通过** —— `panel_list.py` 绿，且 `GET /api/spectrum-devices` 在**本机**返回 9 个输出端点、不含麦克风。
- [ ] **步骤5: 提交**

### 任务 4: 柱宽 8→10px
**文件：** 修改 `hubmon/render.py`；测试 `build/_pool\spec_peak.py`（扩展）
- [ ] **步骤1: 编写失败测试** —— 断言 peak 柱体宽度为 10px（`SPEC_BAR == SPEC_CELL == 10`），且相邻柱间仍留 ≥2px 缝。
- [ ] **步骤2: 运行确认失败** —— 现在 `SPEC_BAR = 8`。
- [ ] **步骤3: 编写最小实现** —— `SPEC_BAR = 10`。
- [ ] **步骤4: 运行确认通过** —— `spec_peak.py`、`spec_led.py` 绿。
- [ ] **步骤5: 提交**

### 任务 5: 风格注册表（重构，无行为变化）
**文件：** 修改 `hubmon/render.py`；测试 复用 `build/_pool/spec_bake.py`
- [ ] **步骤1: 编写失败测试** —— 断言注册表存在且键集合等于 8 种风格，`mirror` 不在其中；每种风格都能通过注册表画出非空帧：
```python
assert set(render.STYLES) == {"led","peak","line","area","sym","grad","dots","fine"}
```
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 新增 `STYLES = {...}`（键 → (chrome 烘焙函数, bars 绘制函数, 是否用幽灵格)），`_spectrum_chrome` / `_spectrum_bars` 改为查表；`mirror` 从表里移除、`_spec_mirror` 删除；`_look_sig()` 里 `spectrum_style` 的加入方式不变。
- [ ] **步骤4: 运行确认通过** —— `spec_bake.py`、`spec_wire.py` 绿（`spec_wire.py` 用 led/mirror 对比 sig，需改成 led/fine）。
- [ ] **步骤5: 提交**

### 任务 6: 配置迁移 `mirror` → `led`
**文件：** 修改 `hubmon/config.py`；测试 `build/_pool/panel_spec.py`（扩展）
- [ ] **步骤1: 编写失败测试** —— 构造含 `"spectrum_style": "mirror"` 的旧 JSON，`Config.load()` 后断言取值为 `"led"`。
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 在 `load()` 里加一张 `{"mirror": "led"}` 的迁移表，`update_from()` 也走同一映射（面板传什么都不至于落一个死值）。
- [ ] **步骤4: 运行确认通过**
- [ ] **步骤5: 提交**

### 任务 7: 风格 `sym` 对称镜像柱
**文件：** 修改 `hubmon/render.py`；测试 `build/_pool/spec_sym.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 断言上下两半以 `SPEC_MID` 对称（同高时上下墨量差 <15%）、柱为**平色**（同一柱内竖直方向 RGB 极差 <10）、柱顶有亮帽、增量成本低（`style_link` 折算 <450 KiB/s）。
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 平色柱自 `SPEC_MID` 向上下各画 `h/2`（`h` 量化到 2px），顶端 2px 混白亮帽；柱宽 10px。
- [ ] **步骤4: 运行确认通过** + `style_link.py` 量成本并记录。
- [ ] **步骤5: 提交**

### 任务 8: 风格 `grad` 单侧渐变柱
**文件：** 修改 `hubmon/render.py`；测试 `build/_pool/spec_grad.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 断言外端比内端亮 ≥20（沿用第一轮 `_spec_strip` 的 ramp）、**同一绝对高度上的像素颜色在两次不同柱高之间保持一致**（位置稳定 ⇒ 只脏跨过的行，这是和已删的 mirror 的关键区别）：
```python
# 高度 40 与 42 两根柱子，比较两者在 y=SPEC_MID-30 处的颜色，应完全相同
```
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 复用 `_SPEC_SEG` 缓存，但按**到基线的绝对距离**取色：`ramp(j)` 的 `j` 是离基线的像素数，柱高变化只暴露新行。
- [ ] **步骤4: 运行确认通过** + `style_link.py` 量成本。
- [ ] **步骤5: 提交**

### 任务 9: 风格 `line` 折线波形
**文件：** 修改 `hubmon/render.py`；测试 `build/_pool/spec_line.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 断言折线只在**上沿**有墨（每个 x 列上最亮的像素 y 随电平单调）、线宽 ≤2px、不填充；并断言成本 <450 KiB/s。
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 24 个点（x = 每段中心，y = 基线 − level×带高），`d.line()` 顺次相连，颜色混白 25%；基线导轨之上不填充。
- [ ] **步骤4: 运行确认通过** + 量成本（**这是本轮唯一"整条横跨全宽"的动态图元，若超门限就先加纵向量化到 4px**）。
- [ ] **步骤5: 提交**

### 任务 10: 风格 `area` 面积山形
**文件：** 修改 `hubmon/render.py`；测试 `build/_pool/spec_area.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 断言填充存在（基线以上有大面积非背景色）、**填充内部随上沿移动而稳定**：把整条曲线整体下移 2px，脏区应只落在上沿附近（复用 `dirty_rects`，断言变化区域高度 <8px）。
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 折线 + `d.polygon` 填充（竖向渐变按绝对高度取色，与 `grad` 共用 ramp）；上沿叠 2px 亮线。
- [ ] **步骤4: 运行确认通过** + 量成本。
- [ ] **步骤5: 提交**

### 任务 11: 风格 `dots` 圆点脉冲
**文件：** 修改 `hubmon/render.py`；测试 `build/_pool/spec_dots.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 断言点亮的是**圆**而非方（圆角处像素落在形状外）、点径随电平增大、静音时不画点。
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 每段一列，列内按电平点亮若干圆点（直径 5px，行距 8px），顶端混白。
- [ ] **步骤4: 运行确认通过** + 量成本。
- [ ] **步骤5: 提交**

### 任务 12: 风格 `fine` 细柱阵列
**文件：** 修改 `hubmon/render.py`；测试 `build/_pool/spec_fine.py`（新建）
- [ ] **步骤1: 编写失败测试** —— 断言柱宽 5px、缝 ≥5px、柱顶 1px 亮头、柱高量化到 2px。
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— 5px 细柱居中于 pitch，柱顶 1px 混白亮头，其余平色。
- [ ] **步骤4: 运行确认通过** + 量成本。
- [ ] **步骤5: 提交**

### 任务 13: 暴露 `spectrum_bands` 到 `/api/stats`
**文件：** 修改 `hubmon/main.py`；测试 `build/_pool/panel_list.py`（扩展）
- [ ] **步骤1: 编写失败测试** —— 断言 provider 返回的 dict 含长度 24 的 `spectrum_bands` 与 `spectrum_peaks`。
- [ ] **步骤2: 运行确认失败**
- [ ] **步骤3: 编写最小实现** —— `run_stream` 的 `state` 里加 `spectrum_bands`/`spectrum_peaks`（由 `dash.spectrum_bands` 取，避免另开一份）。
- [ ] **步骤4: 运行确认通过**
- [ ] **步骤5: 提交**

### 任务 14: 全风格带宽/耗时门限
**文件：** 修改 `build/_pool/style_link.py`、`build/_pool/style_cost.py`；测试 二者自身
- [ ] **步骤1: 编写失败测试** —— 在两个脚本里加硬断言：折算 ≤450 KiB/s、渲染 ≤16.7 ms/帧（沿用现有 8 组合扫描，风格列表改为 8 种）。
- [ ] **步骤2: 运行确认失败/通过** —— 若某风格超门限，回到它的任务里加纵向量化后重测（**门限不过不收尾**）。
- [ ] **步骤3: 编写最小实现** —— 断言与打印。
- [ ] **步骤4: 运行确认通过**
- [ ] **步骤5: 提交**

### 任务 15: 打包 + 冻结 exe 上验证选取器
**文件：** 修改 无（复用 `build.py`）；测试 冻结 exe 自身
- [ ] **步骤1: 编写失败测试** —— 先结束运行中的 `hubmon.exe`（否则 PyInstaller 删旧 exe 报 WinError 5），打包后启动，`GET /api/spectrum-devices` 必须返回非空设备列表 —— **源码通过不算数**，第一轮的 COM 坑只在冻结包里现形。
- [ ] **步骤2: 运行确认失败/通过**
- [ ] **步骤3: 编写最小实现** —— 若 exe 侧枚举为空，检查 `_com_apartment()` 是否在 handler 线程真的被调用。
- [ ] **步骤4: 运行确认通过** —— exe 侧 `GET /api/spectrum-devices` 返回 9 个输出端点，`spec_state=ok`。
- [ ] **步骤5: 提交**（打包产物不入库）

### 任务 16: 实机验证 + 收尾
**文件：** 清理 `build/_pool/` 临时脚本
- [ ] **步骤1:** 用**用户自己的音乐**跑一轮：读 `/api/stats` 的 `spectrum_bands`，确认 24 段里低频不再贴顶（<0.98）、高频段不再趴底（>0.1 的段数 ≥6）；记录 `kb_s`/fps。**绝不启动合成音源。**
- [ ] **步骤2:** 逐个切换 8 种风格，确认画面正常、无报错、面板选取器可切换设备。
- [ ] **步骤3:** 删除 `build/_pool/` 下本轮的一次性脚本（保留 `style_*`/`spec_*` 供回归）。
- [ ] **步骤4:** 更新设计文档的「实测结果」小节，写实测数字。
- [ ] **步骤5:** **攒一次提交并推送**（`-c http.proxy=socks5h://127.0.0.1:7897`，失败重试；成片失败则请用户调整 Clash）。
