# hubmon

把电脑的实时状态投到带屏 USB 集线器的小屏幕上 —— 一个精致、常驻后台的桌面监测仪表盘。

hubmon 在本机后台采样硬件状态，渲染成一帧 320×170 的画面后推送到 Hub 的屏幕上，
并附带一个本地网页控制面板：所有外观与推流选项都能实时调整，改完立即生效、无需重启。

> 已在 **佳点 V1PRO** 带屏集线器（320×170 横屏）上实机验证：稳定 30 fps 以上，占用带宽约 300–360 KiB/s。

## 特性

- **四张大卡片**：CPU、GPU、内存占用、整机功率，数值带滚动动效与指数平滑插值，读数不跳变。
- **水池式占用**：CPU / GPU / 内存卡片按占用比例注水，带行进涌浪、慢速起伏与随负载倾斜的动效，水面与卡片圆角边缘均做抗锯齿。
- **卡片底色律动**：每张卡片内部有一层朦胧的强调色光晕，缓慢呼吸并漂移，位于液面之下；使用背景图片时这层光晕直接呼吸在画面上（可在面板关闭）。
- **型号识别**：自动从 CPU / 显卡名称中提取型号（如 `i5-12400F`、`RTX 5060`）显示在读数正下方；CPU 卡片右上角显示核心·线程数（如 `8C·16T`）。
- **顶栏信息**：主机名、实时上下行网速、当前时间。
- **GPU 温度**：10 秒刷新一次（CPU 温度不采集，原因见「数据来源」）。
- **自定义背景**：默认渐变背景，也可点击「选择图片」从资源管理器挑一张图作为背景，模糊度与亮度可调；面板内置取景框，拖动平移、滑条缩放，所见即所得。使用背景图片时卡片改为**全透明无边框**，整屏读作一张连续的画面。
- **本地控制面板**：iOS 风格单页界面，实时热更新，含「退出程序」按钮。
- **单文件可执行**：PyInstaller 打包，后台运行、无控制台窗口、无任务栏残留。

## 硬件与系统要求

| 项目 | 要求 |
| --- | --- |
| 屏幕 | 带可编程小屏的 USB Hub（320×170 横屏），已在佳点 V1PRO 验证 |
| 系统 | Windows 10 / 11（依赖 Windows 性能计数器与 `nvidia-smi`） |
| 显卡 | NVIDIA 显卡可选；没有时 GPU 相关读数留空，其余指标正常 |
| Python | 3.10 及以上（从源码运行时） |

## 从源码运行

```powershell
git clone https://github.com/<你的用户名>/hubmon.git
cd hubmon

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python run.py
```

首次运行前请先关闭厂商标配的控制工具（它会独占设备）；确实需要同时运行时可加 `--force`。

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--fps N` | 覆盖帧率上限，`0` 表示不限速（默认读取配置） |
| `--demo` | 使用合成数据，无需真实硬件 |
| `--full` | 始终发送整帧（关闭局部更新） |
| `--preview DIR` | 只渲染 PNG 预览到目录，不连接设备 |
| `--frames N` | 预览模式生成的帧数（默认 120） |
| `--port N` | 指定控制面板端口 |
| `--no-browser` | 启动时不自动打开浏览器 |
| `--force` | 即使厂商工具在运行也强制推流 |

### 无需设备预览效果

```powershell
python run.py --preview out --demo
```

会在 `out/` 下生成若干放大后的 PNG，用来检查配色与布局。

## 控制面板

程序启动后会自动打开 `http://127.0.0.1:8756/`（端口被占用时自动顺延）。
面板分为以下几组：

- **推流**：帧率上限、局部更新开关、数值与帧率刷新间隔。
- **文字动效**：数值滚动开关与速度。
- **背景与动效**：卡片底色律动开关，以及背景图片（选择 / 清除 / 取景框缩放与平移 / 模糊度 / 亮度）。
- **外观**：亮度、背景亮度、饱和度、玻璃质感、各指标强调色、显示项开关。
- **面板**：面板端口、是否自动打开浏览器。
- **外部工具**：PresentMon 可执行文件路径。

> 背景图片的取景框只有在设置了图片之后才出现；「选择图片」会弹出 Windows 的文件选择对话框。

改动即时生效于面板预览；点击「保存设置」后才会应用到屏幕并写入配置文件，下次启动继续生效。

## 配置

配置保存在 `%APPDATA%\hubmon\config.json`，字段与面板一一对应，可直接编辑。
命令行传入的参数只对本次运行生效，不会写回文件。

## 设备接入与厂家接口

`docs/vendor-api/` 原样收录了厂家提供的两份接口文档，作为后续开发的基础：

- `v1pro-developer-api.md` —— 开发者 API 文档：本地 HTTP 服务、USB 使用权移交、底层 USB 函数与型号兼容范围。
- `v1pro-local-api.openapi.json` —— OpenAPI 3.0 描述文件，可直接导入 Postman / Apifox 等工具。

要点：

- 厂商标配 GUI 在本机 `http://127.0.0.1:8765` 提供 HTTP 服务（`POST /live` 提交实时文字、`GET|POST /usb/status` 查询设备状态）。
- `POST /usb/handoff/{start,keepalive,finish}` 提供 USB 使用权租约：拿到租约后可以合法地接管设备，**不必强杀厂商进程**。
- 型号以设备返回的信息为准（V1 / V1PRO / V2 的端点与画面尺寸各不相同），文档同时公开了 RGB565 大端格式与 LIVE 协议。
- 文档引用的 `v1pro-usb-protocol.md`、`v1pro-compressed-transfer.md`、`gfm3-persistent-lz4.md` 本次未提供。

hubmon 目前仍采用直连 USB 推流的方式；若厂商 GUI 正在运行，请先退出，后续可考虑接入上面的租约接口。

## 数据来源

| 指标 | 来源 |
| --- | --- |
| CPU 占用 / 频率 | `psutil` + Windows PDH 性能计数器（`% Processor Performance` × 基准频率） |
| CPU 核心 / 线程 | `psutil.cpu_count()` |
| 内存 | `psutil` |
| GPU 占用 / 频率 / 功耗 / 温度 / 显存 | `nvidia-smi` |
| 网速 | `psutil` 网卡计数器，1 秒滑动窗口 |
| 前台窗口帧率 | [PresentMon](https://github.com/GameTechDev/PresentMon)（可选） |
| 整机功率 | CPU 占用估算 + GPU 实测功耗 + 平台基线功耗，属**估算值** |

CPU 温度不在此列：纯用户态下读不到核心温度，回退到主板 ACPI 热区的读数既不准也不稳定，
需要 LibreHardwareMonitor 之类的工具在后台常驻才有意义，所以不再采集。

## 打包为单文件 exe

```powershell
pip install pyinstaller
python build.py
```

输出 `dist/hubmon.exe`：单文件、无控制台窗口，双击即用。
本机若存在 PresentMon 会被一并打包，否则该功能留空。

## 项目结构

```
run.py              入口（供 python 直接运行与 PyInstaller 使用）
build.py            打包脚本
requirements.txt    运行依赖
docs/vendor-api/    厂家提供的接口文档（原样收录）
hubmon/
  main.py           主循环、命令行解析、增量更新调度
  sensors.py        后台采样线程（CPU / GPU / 内存 / 网速 / 帧率）
  render.py         画面渲染与位图转换
  transport.py      屏幕输出
  webui.py          本地网页控制面板
  config.py         配置定义与 JSON 持久化
```

## 许可

[GPL-3.0](LICENSE)。
PresentMon 为 Intel 的独立开源项目，遵循其各自许可；本项目仅在运行/打包时选择性地调用或随附。