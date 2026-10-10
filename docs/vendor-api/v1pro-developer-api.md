# 佳点 V1 和 V2 开发者 API 文档

文档版本：0.1.0。更新日期：2026 年 10 月 10 日。

面向第三方桌面程序、本地网页和设备集成开发者。当前 GUI 提供本机 HTTP 服务，可提交实时文字、查询 GUI 管理的设备状态，并协商 USB 使用权。图片、亮度、实时帧和素材传输已有底层通信实现，但尚未封装成统一 HTTP API。

本文的“已实现”指当前项目源码中的接口。旧安装包可能没有相同接口；接入时先检查 `/usb/status`。HTTP 接口描述见同目录 [OpenAPI 文件](./v1pro-local-api.openapi.json)，可导入支持 OpenAPI 3.0 的接口工具。后文的规划接口尚不可调用。

## 接入方式和能力

| 能力 | 当前接入方式 | 状态 |
| --- | --- | --- |
| 实时文字 | GUI 本地 HTTP `POST /live` | 已实现，异步提交 |
| GUI 管理的设备状态和设备列表 | `GET /usb/status` 或 `POST /usb/status` | 已实现，不是完整 USB 扫描接口 |
| USB 使用权移交与归还 | `/usb/handoff/start`、`keepalive`、`finish` | 已实现 |
| 亮度和息屏 | Python USB 函数或 USBDL 协议 | 底层已实现，无统一 HTTP 接口 |
| 实时整帧和局部画面 | Python USB 函数或 USBDL LIVE 协议 | 底层已实现，依赖固件能力 |
| 图片和动画写入 Flash | 现有上位机传输器；内部 WebUSB 参考实现 | 无统一 HTTP 上传接口 |
| 任务编号、设备显示结果查询、取消 | 见规划章节 | 尚未实现 |

一般桌面集成从本地 HTTP 开始。需要自行控制 USB 时，使用完整序列号取得租约后再打开设备。现有 WebUSB 源码包是内部参考包；本 API 文档不包含 GUI、固件或 SDK 源码交付。

## V1 和 V2 兼容范围

项目中“V1”与“V1PRO”是不同硬件标识。不要把第一代 V1 的端点直接当作 V1PRO 使用。

| 型号 | 硬件标识 | 固件型号标识 | 默认画面尺寸 | 主机 Bulk 应答端点 |
| --- | --- | --- | --- | --- |
| 佳点 V1 | `V1` | `V1X0` | 320 × 170 | `0x82` |
| 佳点 V1PRO | `V1P` | `V1PR` | 320 × 170 | `0x81` |
| 佳点 V2 | `V2` | `V2PR` | 320 × 240 | `0x81` |

以上是当前主机设备配置的默认值。实际画面尺寸、容量和压缩能力以已连接设备返回的信息为准。本地 HTTP 的设备状态与移交使用 GUI 已识别的设备；实时文字仍要求目标固件支持 LIVE，不能仅凭型号保证支持。本文的直接 USB 示例以 V1PRO / V2 应用固件为目标，原始 V1 需要对应的端点适配与固件验证。

当前 V1 兼容固件提供 GFM1、原始 RGB565 LIVE 全屏、局部区域和批量区域，以及亮度查询与设置；不提供 GFM2 / GFM3 / LZ4 压缩。V1 的 Flash 常见为 256 KiB 或 512 KiB，不能套用 V1PRO / V2 的 MB 级动画容量。读取 JED 的 `B=` 总字节数、`U=` 可用字节数和实际帧数；OTA 暂存与回滚区域已经从可用素材空间中扣除。当前容量布局下，256 KiB 版约可保存 1 张全屏 GFM1，512 KiB 版约 3 张，以实际 `maxPayloadBytes` 为准。

V1PRO 与 V2 的 USB VID 通常为 `0x0483`，PID 可为 `0x66AA` 或 `0x66AB`；VID/PID 不足以识别型号或区分多台设备。使用设备的完整 24 位十六进制序列号。`HW` 标识、设备返回的屏幕尺寸和压缩能力都需要校验；发生身份冲突时不要猜测型号。

## 本地 HTTP 约定

基础地址：`http://127.0.0.1:8765`。GUI 主实例运行时启动服务，仅监听本机回环地址；设备子进程的动态端口和内部凭证不属于第三方接口。

请求体使用 UTF-8 JSON 对象，建议发送 `Content-Type: application/json`。服务读取的请求体上限为 **16,384 字节**，包括 JSON 语法和 UTF-8 编码后的文字，不是 16,384 个字符。发送非空请求体时应提供有效 `Content-Length`；不使用分块请求体。响应为 JSON，带 `Cache-Control: no-store`。

当前主接口没有开发者 API Key。USB 移交的 `leaseId` 是租约凭证，需要保存在调用方内存中。浏览器请求还受来源检查限制：

| 来源 | 当前行为 |
| --- | --- |
| 不带 `Origin` 的桌面程序请求 | 允许 |
| `http://localhost`、`http://127.0.0.1`，可带端口 | 允许 |
| `https://jadot.cn`、`https://www.jadot.cn`、`https://jadot.club`、`https://www.jadot.club` | 允许 |
| 其他网页来源，包括 `Origin: null` | HTTP 403 |

浏览器预检使用 `OPTIONS`，成功返回 204，声明允许 `GET, POST, OPTIONS` 和 `Content-Type`。任意第三方 HTTPS 网站目前不能直接跨域调用这个服务。接入其他网页来源需要增加明确的授权机制；不要移除现有来源检查。

### 快速开始

先启动 GUI 并连接目标设备。状态查询只读取软件状态：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/usb/status'
```

提交文字会改变当前设备画面。先停止 GUI 的时钟、仪表盘、歌词、频谱、桌宠、在线播放等占用模式：

```powershell
$payload = @{ text = 'Hello from my application' } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/live' `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($payload))
```

返回 `{ "ok": true }` 表示请求已交给 GUI，**不表示设备已显示成功**。当前没有用于查询这次文字显示结果的 HTTP 接口；结果可在 GUI 的实时文字状态和日志中查看。

## 实时文字接口

`POST /live`

```json
{ "text": "CPU 35%\nTemperature 48 C" }
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | 建议必填；缺省时服务传入空字符串。支持换行，按 GUI 当前样式渲染 |

成功响应 HTTP 200：

```json
{ "ok": true }
```

接口不接受设备选择、字体、颜色、坐标、图片或任务编号等参数。它提交到 **主窗口对应的 core**，没有按 `serial` 路由到设备子标签页的能力；多设备场景不要把它当作指定 SN 的文字接口。

文字提交是异步操作。如果 GUI 未连接设备或其他显示模式正在运行，HTTP 仍可能返回 200，随后在 GUI 中显示未执行原因。上一条文字正在发送时，新的提交会覆盖待发送文字，仅保留最新一条；不保证每条通知都显示。`POST /` 是现有兼容别名，新接入请使用 `/live`。

## 设备状态接口

`GET /usb/status?serial={SN}` 或 `POST /usb/status`

POST 示例中的 SN 全零为格式占位，调用时替换为实际序列号：

```json
{ "serial": "000000000000000000000000" }
```

不传 `serial` 时返回主实例状态及 `devices` 列表；传入时选择对应设备。示例响应仅展示字段形状，容量数值以实际响应为准：

```json
{
  "ok": true,
  "guiRunning": true,
  "apiVersion": 2,
  "connected": true,
  "serial": "000000000000000000000000",
  "busy": false,
  "busyReason": "",
  "handoffActive": false,
  "handoffError": "",
  "ownershipState": "software",
  "ownershipMessage": "软件已连接，可交接给网页",
  "leaseExpiresAt": 0,
  "vid": 1155,
  "pid": 26282,
  "hardwareVariant": "V2",
  "panelWidth": 320,
  "panelHeight": 240,
  "maxMaterialFps": 45,
  "maxFrames": 109,
  "maxPayloadBytes": 16773120,
  "capabilities": { "multiDeviceHandoff": true, "explicitSerial": true },
  "devices": []
}
```

| 字段 | 含义 |
| --- | --- |
| `apiVersion` | USB 移交路由版本，当前为 2；不是整套产品 SDK 或文档的版本 |
| `devices` | GUI 管理的主设备和设备子进程状态；未连接主设备可能不在列表内 |
| `connected` | GUI 保留逻辑连接，不等同于第三方已取得 USB 句柄 |
| `serial` | 大写完整 SN；未确认身份时可能为空 |
| `busy`、`busyReason` | GUI 当前是否因任务占用而不能移交 |
| `handoffActive` | 有效 USB 移交租约是否存在 |
| `handoffError` | 最近的移交错误信息 |
| `ownershipState` | `software` 软件管理、`handoff` 正在交接、`web` 租约使用中、`disconnected` 未连接 |
| `ownershipMessage` | 用于显示的人类可读说明，不作为稳定错误码解析 |
| `leaseExpiresAt` | 当前租约到期时间，Unix 毫秒；无租约时为 0 |
| `vid`、`pid` | 十进制 USB 标识 |
| `hardwareVariant` | 已识别硬件，如 `V1`、`V1P`、`V2` |
| `panelWidth`、`panelHeight` | 实际画面尺寸，像素 |
| `maxMaterialFps` | 主机对当前设备配置的素材帧率上限，不保证实时刷新可达到该速度 |
| `maxFrames` | 容量对应的参考帧数，不是所有压缩动画的固定帧数上限 |
| `maxPayloadBytes` | 当前允许的素材载荷字节数 |
| `capabilities` | 路由能力；当前不包含 GFM3、LZ4L 等固件压缩能力 |
| `available` | 部分子进程异常状态返回 `false`；普通状态可能没有此字段 |

身份、画面尺寸和容量尚未确认时，相应字段会省略。查询一个 GUI 未管理的 SN，可能返回 HTTP 200 且 `ok=true`，但同时 `connected=false`、`busy=true`；必须检查状态字段。不要用 `/usb/status` 代替物理 USB 扫描或实时硬件读回。

## USB 使用权移交

移交只暂时暂停 GUI 对目标设备的 USB 使用。第三方仍需自己打开 USB 接口并完成协议操作；HTTP 服务不会替第三方传输素材。

### 申请租约

`POST /usb/handoff/start`

```json
{
  "serial": "000000000000000000000000",
  "clientId": "my-dashboard-session-unique-id",
  "ttlSeconds": 90
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `serial` | string | 建议始终填写实际完整 SN，大小写不敏感。省略时仅在 GUI 恰好管理一台已连接设备时自动选择 |
| `clientId` | string | 建议每个调用会话使用唯一值；最多保留 128 字符。同一非空 clientId 可复用并续期当前租约，不是身份认证密钥 |
| `ttlSeconds` | number | 默认 90 秒，核心限制到 30～180 秒 |

新租约成功响应 HTTP 200：

```json
{
  "ok": true,
  "leaseId": "example-opaque-lease-token",
  "expiresAt": 1893456090000,
  "serial": "000000000000000000000000",
  "pid": 26282
}
```

复用租约时可能增加 `reused=true`，且省略 `pid`。`expiresAt` 为 Unix 毫秒。只有 HTTP 200、`ok=true` 且拿到非空 `leaseId` 后，才能尝试打开所选设备。

移交会等待 GUI 释放 USB 资源，建议调用超时设置为至少 15 秒。素材转换、下载、固件操作、扫描、实时显示或另一个租约正在进行时，可能返回 409；结束冲突任务后再尝试，不要强行抢占。

### 保持租约

`POST /usb/handoff/keepalive`

```json
{
  "leaseId": "example-opaque-lease-token",
  "serial": "000000000000000000000000"
}
```

成功响应：

```json
{
  "ok": true,
  "leaseId": "example-opaque-lease-token",
  "expiresAt": 1893456120000
}
```

`leaseId` 必填。`serial` 可省略，但建议带上以核对目标。每次成功续期会把核心租约更新为 **90 秒**，不继续使用最初的 `ttlSeconds`。建议每 20 秒续期一次。

### 归还设备

`POST /usb/handoff/finish`

请求字段同 keepalive，成功返回：

```json
{ "ok": true, "released": true }
```

**先释放第三方持有的 USB 接口并关闭句柄，再归还租约。** 只调用 finish 不会关闭调用方的 USB 句柄，否则 GUI 恢复后仍可能遇到“设备被占用”。

成功归还后约 180 秒内，路由通常接受同一租约再次 finish 并返回 `{ "ok": true, "released": false }`。未知、过期、目标已退出或与 SN 不匹配的租约会返回 409，不能假定任意 finish 请求都是幂等成功。

### 调用顺序和失败处理

1. 查询状态，明确选择完整 SN，确认目标在线且无冲突任务。
2. 申请租约并保存 `leaseId`；网络超时后先查询状态，不要立即打开 USB。
3. 打开并核对同一 SN 的设备，再开始协议操作；持续续期。
4. 在成功、失败、取消的清理路径中，先释放 USB，再 finish。
5. 续期失败、设备拔出或连接身份变化时，停止写入并关闭句柄；重新查询、选择和申请租约。

租约超时会使 GUI 恢复 USB 管理，不会自动关闭第三方仍持有的句柄。`clientId`、`leaseId` 和状态查询也不会授予其他网页来源跨域访问权。

## HTTP 错误处理

常见错误响应：

```json
{ "ok": false, "error": "GUI 未连接所选设备" }
```

| HTTP 状态 | 当前语义 | 调用方处理 |
| --- | --- | --- |
| 200 | 请求处理成功或文字已提交 | 继续检查 `ok` 和设备状态，不将其等同于硬件完成 |
| 204 | OPTIONS 预检成功 | 浏览器可继续发送允许的请求 |
| 400 | 请求体或参数格式错误 | 修正 JSON、长度或 SN |
| 403 | 网页来源不允许；私有子接口也可能凭证不匹配 | 使用允许的接入来源，不访问子进程私有端口 |
| 404 | 接口不存在或当前 GUI 没有该能力 | 核对路径和 GUI 版本 |
| 409 | 忙碌、目标不匹配或租约失效 | 检查目标和占用状态，不盲目循环重试 |
| 500 | 回调异常 | 保留错误并检查 GUI 日志 |
| 503 | 目标进程未响应、GUI 暂未响应或资源释放失败 | 关闭调用方句柄，检查对应设备连接 |

GUI 未启动、端口被占用或退出时，客户端可能收到连接失败或超时，而不是 HTTP JSON 错误。当前错误以 `error` 文字表达，没有统一机器错误码；不要依赖精确中文字符串作业务分支。

## Python 桌面程序示例

以下例子只依赖 Python 标准库，不直接打开 USB。执行时会向主窗口设备提交文字；设备未就绪时抛出错误。它不会申请移交租约，因为文字由 GUI 自己发送。

```python
import json
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8765"

def call(path, payload=None):
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False
    ).encode("utf-8")
    request = Request(BASE + path, data=data,
                      headers={"Content-Type": "application/json"},
                      method="GET" if payload is None else "POST")
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "API request failed"))
    return result

state = call("/usb/status")
if not state.get("connected") or state.get("busy") or state.get("handoffActive"):
    raise RuntimeError("主窗口设备未就绪或正在被占用")
call("/live", {"text": "Hello V1PRO / V2"})
print("文字已提交，请在 GUI 中查看显示结果")
```

状态检查和文字提交之间仍可能发生设备状态变化；现有接口不提供原子的任务预约，也不提供显示完成确认。

## 底层 USB 接入参考

这一节是已有内部函数与协议的接入参考，不代表已经发布了可安装的公共 Python SDK。独立 USB 程序需要自行管理驱动、完整 SN、串行命令队列和句柄生命周期。运行前应关闭 GUI，或完成前述租约移交。

### 设备查询和背光

模块 `usb_flash_query.py`：

| 函数 | 主要参数 | 返回 |
| --- | --- | --- |
| `query_board_flash_info(vid, pid, *, device_path, timeout_ms=2000, attempts=3)` | 明确的设备路径，如 `pyusb://sn/{实际SN}` | `BoardFlashInfo`，包含尺寸、容量、硬件与压缩能力 |
| `ping_device(vid, pid, *, device_path, timeout_ms=800)` | 同一设备路径 | bool |
| `send_display_brightness(vid, pid, *, brightness, device_path, timeout_ms=2000)` | 设备亮度值 0～255；0 表示息屏 | bool，表示是否收到合法 DSP 应答 |
| `send_display_mode(vid, pid, *, screen_off, brightness=None, device_path, timeout_ms=2000)` | 息屏/开屏 | bool |
| `query_display_status(vid, pid, *, device_path, timeout_ms=2000)` | 查询背光 | `DisplayStatus`，旧固件可能不支持 |

亮度 0～255 是设备值，HTTP 规划里的百分比不是该函数的当前参数。需要持久化的设置应读回验证；不能仅凭主机写入成功保证掉电后保留。

### 实时画面

模块 `usb_live_frame.py`：

| 函数 | 用途 |
| --- | --- |
| `configure_panel(lcd_w, lcd_h)` | 在编码前配置实际屏幕尺寸 |
| `send_live_rgb565(pixels, *, device_path, usb_chunk=4096, timeout_ms=3000)` | 全屏 RGB565；字节数必须等于宽 × 高 × 2 |
| `send_live_region_rgb565(pixels, *, x, y, width, height, device_path, ...)` | 更新合法矩形区域；字节数等于区域宽 × 高 × 2 |
| `send_live_rgb565_compressed(pixels, *, device_path, ...)` | 分块 LZ4 全屏；仅在设备返回 `LZ4L` 能力时使用 |
| `exit_live_mode(*, device_path, timeout_ms=2000)` | 退出 LIVE 保持，恢复离线动画 |

RGB565 像素按行排列，每像素先发送高字节，再发送低字节。例如红色 `0xF800` 发送 `F8 00`。LIVE 画面直接送 LCD，不写素材 Flash；USB 断开后不保证继续显示该实时内容。发送函数返回设备应答，异常应中止当前会话。

`configure_panel` 会修改该模块的全局尺寸；一个进程不要交错使用不同尺寸设备的直接帧发送。不同设备采用隔离会话或进程，每台设备内部串行发送命令，尤其不要让心跳读取抢走传输 ACK。

### 素材传输和压缩格式

先读取设备能力和容量，再编码素材。`GFM3` / `LZ4P` 是离线动画压缩能力，`LZ4L` 是实时帧压缩能力；不能相互替代。支持 GFM3 的设备不一定支持 GFM1，GFM3-only 固件必须选择兼容格式。

本项目 `tools/webusb/` 中的 `V1ProWebTransfer` 提供 `connect`、`disconnect`、`refreshStorageInfo`、`transferFile`、`transferAlbum`，当前这份内部参考实现使用 **GFM1**。因此不能把它当作覆盖所有现行固件的公共 SDK，也不能把它与官网其他检出的 SDK 版本混用。

该参考实现的 `transferFile()` 默认只等待主机发送完成；`transferAlbum()` 使用 `waitForCommit: true` 等待完成应答。设备确认与掉电后的素材保留是不同验收条件，持久化仍应独立读回并做断电重连检查。再次上传素材会替换当前素材区域。

协议字段和数据容器见 [USB 协议](./v1pro-usb-protocol.md)、[压缩传输](./v1pro-compressed-transfer.md) 和 [GFM3 分块压缩](./gfm3-persistent-lz4.md)。这些历史文档中的默认格式、容量和兼容结论，应结合目标固件返回能力使用。

## 统一 HTTP API 规划

**本节是设计草案，所有下列路径尚未实现，不包含在当前 OpenAPI 文件中。** 请求这些路径目前返回 404。现有 `/live` 和 USB 移交接口继续使用各自的当前路径与语义。

拟采用 `/api/v1` 前缀，按完整 SN 路由：

| 拟定方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/devices` | 查询统一设备列表、身份与能力 |
| `GET /api/v1/devices/{serial}` | 查询指定设备 |
| `POST /api/v1/devices/{serial}/text` | 指定设备显示文字，并返回任务编号 |
| `POST /api/v1/devices/{serial}/image` | 上传并实时显示图片 |
| `POST /api/v1/devices/{serial}/frames` | 提交实时画面，支持有界队列与丢弃旧帧策略 |
| `PUT /api/v1/devices/{serial}/display` | 亮度百分比、开屏和息屏 |
| `POST /api/v1/devices/{serial}/materials` | 写入离线素材并返回任务编号 |
| `GET /api/v1/tasks/{taskId}` | 查询执行进度、设备确认和最终错误 |
| `POST /api/v1/tasks/{taskId}/cancel` | 在协议支持的阶段取消任务并清理 USB |

拟定异步响应 HTTP 202：

```json
{
  "ok": true,
  "taskId": "example-task-id",
  "serial": "000000000000000000000000",
  "state": "queued"
}
```

拟定任务状态为 `queued`、`running`、`waiting_device_ack`、`succeeded`、`failed`、`cancelled`。`succeeded` 应以对应操作的设备确认定义；需要掉电保留的操作还应提供明确的持久化状态，不能用 HTTP 202 或发送进度 100% 代替。

规划接口还需要接入授权、按设备的 USB 调度、稳定机器错误码、请求大小与帧率限制，并完成 V1 / V1PRO / V2 的兼容验证。固件升级不属于这一版通用显示 API 的规划范围。

## 接口版本和接入检查

1. GUI 启动后，读取 `/usb/status`，检查移交 `apiVersion` 和路由能力。
2. 按实际 SN 确认型号、画面尺寸、容量与固件能力，避免把缺省值当作读回值。
3. 分别验证 GUI 离线、设备离线、设备忙碌、拔出、租约过期和两台设备连接的处理。
4. 实时文字检查 GUI 的执行结果；直接 USB 操作检查目标设备 ACK；离线素材另外检查重新上电后的播放。
5. 同一设备只保留一个 USB 所有者；正常完成和失败路径都关闭第三方句柄后归还租约。

当前文档描述本地接口，不代表已发布新的 GUI 安装包、公共 SDK 或网站 API 服务。
