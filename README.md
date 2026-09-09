# Infinity（Cursor Grok Bot 工具箱）

管理 Cursor 账号池、批量领取 **Grok Bot（Sand）** 资格、给本机 Cursor 打客户端模式补丁并一键切号的桌面小工具。基于 pywebview（Windows 走 Edge WebView2），界面是 `web/` 下的深色仪表盘，Python 侧通过 `window.pywebview.api` 把能力暴露给前端。

> **仅供学习研究与技术交流，严禁倒卖或收费。** 本项目与 Cursor / Anysphere、xAI / Grok 无任何关联；使用可能违反相关服务条款，风险与后果由使用者自行承担。使用前请先读 [免责声明](DISCLAIMER.md)。

## 功能

- **本机 Cursor 补丁**：打补丁 / 回退 / 「查看补丁情况」；路径留空自动检测，也可手填 `Cursor.exe` 或安装目录。非管理员运行时自动走 UAC 提权子进程。
- **接入 Grok Bot Box 网关**：3.19.13 起 Sand 流量必须经账号自己的云端 Box 代转（见下文）。打补丁时自动接入当前登录账号；切号后自动换成新账号的 Box；票过期由注入的 JS 自刷新；也可单独点「接入 Box 网关」/ 运行 `python sand_patch.py provision-box`。
- **修复 DNS**：本机开着 Clash 等 fake-ip 代理时，写 hosts 修好 Cursor 域名；另有 DoH 兜底解析（见 `resolve.py`）。
- **两种 token 自动识别**：`access_token`（JWT，`eyJ...`）与 `ws token`（`user_01XXXX::eyJ...`，即 WorkosCursorSessionToken）。
- **导入方式**：直接粘贴（每行一个，可混排）、粘贴 `cursor_accounts_*.json` 内容、「导入文件」选一个/多个 JSON、或「探测本机账号」把当前 Cursor 已登录的号加进来。按 user id 自动去重，账号持久化到 `%LOCALAPPDATA%\SandClaimer\accounts.json`。
- **账号列表**：标签、分组筛选、套餐、Sand 状态与额度一屏可见；并发可调（1-10），支持批量领取与批量刷新状态。
- **批量领取**：前端逐个调用并实时显示每行状态；已开通短路、团队号自动带上 `teamId`、个人号走试用、免费号标记「需绑卡」。
- **切号**：把所选账号写入本机 Cursor 登录态（会先关闭正在运行的 Cursor 再重开），可勾选「同时重置机器码」避免多个小号被关联。导入过 refresh token 的账号会一并写入，让 Cursor 能自行续期。
- **账号详情**：只读查询账期额度、API/Auto 分项与按模型花费。
- **打开登录浏览器**：用 CDP 把会话 cookie 注入独立 profile 的 Chrome/Edge 并落到领取页，方便手动绑卡或领取。

## 运行（开发）

```bat
python -m pip install -r requirements.txt
python app.py
```

> Windows 需要 **Edge WebView2 运行时**（Win10/11 一般自带；缺失时到微软官网装「Evergreen WebView2 Runtime」）。

## 打包（Nuitka 编译 + 安装包）

双击或命令行运行：

```bat
build.bat
```

`build.bat` 会依次：装依赖 → 修补 Nuitka 的 pywebview 插件 → 生成图标 → Nuitka 编译 → Inno Setup 打安装包。

产物：

- `nuitka-out\SandClaimer.exe` —— 单文件绿色版，双击即用。
- `installer\Infinity-Setup-2.4.0.exe` —— 中文安装向导，装到 Program Files 并建开始菜单 / 桌面快捷方式。

本机补丁已适配 **Cursor 3.17.21 / 3.18.9 / 3.18.25 / 3.19.7 / 3.19.13**（按安装目录自动选锚点，旧版适配保留）。

### Grok Bot Box relay（2.4.0，Cursor 3.19.13 / Grok Bot 0.44.0 起必需）

2026-09-09 起服务端不再接受「Cursor 登录票 + `sand` 身份」直连 `api2.cursor.sh` 的 `InferenceService/Stream`（回 `401 ERROR_NOT_LOGGED_IN` / `Sand traffic is not supported on this endpoint`），光改客户端身份已经没用。可行路线是让 Grok Bot 的**云端 Box** 代转，2.4.0 把这条链路做进了工具：

1. `grok_box.py` 用本机 Cursor 登录票调 `aiserver.v1.GrokBotService/EnsureSandBox`，拿到该账号 Box 的网关地址 + 短期票，写到 `SandClientModeStream/sand-client-cli/grok-box-relay.json`（Windows 在 `%LOCALAPPDATA%`，macOS 在 `~/Library/Application Support`；与 cursor-sdk2api 的 `SAND_BOX_RELAY_SOURCE=file` 共用同一份文件）。Box 默认没有 relay 路由，首次会通过 Box 网关的 `/api/createAgent` + `/api/sendPrompt` 让 Box 内的 Agent 自己把 `/sand-stream-relay/aiserver.v1.InferenceService/Stream` 路由装到 `host-main.cjs`，然后轮询探活（首次可能要几分钟）。
2. 补丁往 `cursor-agent-host` / `cursor-always-local` 两处 `TransportFactory.applyAuthorization` 各注入一段 `/*SAND_GROK_BOX_RELAY_AUTH_V1*/`：只拦 `InferenceService/Stream`，每次请求重读描述符，把 URL 改到 Box 网关的 relay 路由、`Authorization` 换成 Box 票，并带上 Grok Bot 0.44 的 `sand` 身份；票快过期时用描述符里的 Cursor 票自己再 mint 一次并回写文件，不用重启 Cursor。marker 与 SandClaimer 1.4.2 / sand_stream_installer 同名，能原地接管它们注入的旧版静态块。
3. 3.19.13 的会话工厂从 4883.js 的 `ve()` 改名为 4884.js 的 `me()`，锚点已补上并加了按形状兜底；其余 3.19.7 规则在 3.19.13 上原样命中。

Box 票按账号发，用哪个号的 Box 就扣哪个号的 Grok Bot 额度，所以「切号」会先给新账号接入 Box 再启动 Cursor。「查看补丁情况」里 `grok_relay` 是注入处数、`box_relay` 是描述符状态；描述符缺失时 Cursor 里的 Sand 对话会报 `SAND_GROK_BOX_RELAY_CONFIG_INVALID`，点「接入 Box 网关」即可。回退补丁会连描述符一起删（里面有 Cursor 登录票）。

只想编译不打安装包时用 `build_win.bat`：默认出 onefile，`fast` 出 standalone 目录（跳过 onefile 打包，快），`deps` 先装依赖再编译。云端打包走 GitHub Actions（`.github/workflows/build.yml`）：推 `v*` tag 或在 Actions 页手动触发，同时产出 Windows 安装包和 macOS 的 Apple Silicon / Intel 两个 dmg，在该次运行的 Artifacts 里下载。macOS 的 `.app` 无法在 Windows 上交叉编译，也可改用 Codemagic（`codemagic.yaml`）。

### 为什么用 Nuitka（而非 PyInstaller）

- **启动更快**：Python 源码被编译成 C/机器码，不是解释执行的 `.pyc`。
- **天然混淆/加密**：产物是原生机器码，源码不可还原；onefile 运行时把负载解压到临时目录再执行（相当于加密封装），比 PyInstaller 的可直接解包 `.pyc` 强得多。
- 需要本机装有 **MSVC（VS2022 Build Tools）** 供 Nuitka 编译；首次编译较慢，之后走 clcache 缓存会快很多。

> `patch_plugin.py`：Nuitka 的 pywebview 插件（截至 4.1.3）在 Windows 白名单里漏了 pywebview 6.2.x 新增的 `webview.platforms.win32`，会导致打包后 winforms 后端起不来。该脚本幂等地把它补进白名单，`build.bat` 已自动调用。
>
> `ChineseSimplified.isl`：安装向导的简体中文语言包（Inno Setup 默认不含）。

## 领取规则（与 Cursor 官方一致）

- **付费账号**（Pro+ / Ultra / Team）：直接开通，无需绑卡。
- **免费账号**：领取需先验证信用卡，工具会标记「需绑卡」（如返回验证链接会一并给出），可用「打开登录浏览器」手动完成。
- **团队账号**：走团队通道并自动带上 `teamId`（从 `get-me` 读取）。团队级开通是否覆盖全部成员座位，取决于 Cursor 侧策略。

## 用到的官方接口（2026 实测）

| 用途 | 方法 | 端点 | 鉴权 |
|---|---|---|---|
| 查资格 | POST | `cursor.com/api/dashboard/get-sand-access-status` | 会话 cookie + Origin |
| 查 Bot 额度 | POST | `cursor.com/api/dashboard/get-sand-usage-status` | 会话 cookie + Origin |
| 查总额度 | GET | `cursor.com/api/usage-summary` | 会话 cookie |
| 取 teamId | POST | `cursor.com/api/dashboard/get-me` | 会话 cookie + Origin |
| 个人领取 | POST | `cursor.com/api/dashboard/start-sand-trial` | 会话 cookie + Origin |
| 团队领取 | POST | `cursor.com/api/dashboard/request-sand-team-access`（body `{teamId}`） | 会话 cookie + Origin |
| 账期消费 | POST | `cursor.com/api/dashboard/get-current-period-usage` | 会话 cookie + Origin |
| 套餐名 | GET | `api2.cursor.sh/auth/full_stripe_profile` | Bearer accessToken |
| 按模型花费 | POST | `api2.cursor.sh/aiserver.v1.DashboardService/GetAggregatedUsageEvents` | Bearer + `application/proto` |
| 申请 Box 网关 | POST | `api2.cursor.sh/aiserver.v1.GrokBotService/EnsureSandBox` | Bearer + `application/proto` + Grok Bot 0.44 `sand` 身份头 |

`cursor.com` 的 dashboard 系列即使是读也要带 Origin 过 CSRF，否则 403；`api2` 的一元接口必须用 `Content-Type: application/proto`，发 JSON 会 400/415。

## 绕过本机 DNS 劫持

有些本地网关工具（如 cgw、Clash fake-ip）会把 `cursor.com` / `api2.cursor.sh` 的 DNS 指向本机中转 IP，导致请求被拦截或用错账号。工具内置 DoH（1.1.1.1）解析这两个域名的真实 IP，仅对它们覆盖 `socket.getaddrinfo`，TLS 的 SNI 与证书校验仍用原域名，安全性不受影响。界面上的「修复 DNS」则是把结果写进 hosts。

## 安全

- token 只在本机内存与本机↔Cursor 官方之间使用，不上传任何第三方服务；本项目没有服务端，也没有遥测上报。
- 读本机 Cursor 登录态时以只读方式打开 `state.vscdb`（`mode=ro&immutable=1`），Cursor 正在运行也能读且绝不写盘。
- 请勿把含 token 的 JSON 或本工具日志分享给他人。

## 声明

本项目仅供学习研究与技术交流，与 Cursor / Anysphere、xAI / Grok 及其关联公司无任何隶属、授权或合作关系，相关名称与商标归各自权利人所有。

工具会修改本机客户端文件、hosts 与本机标识，并用你自己提供的凭据调用官方接口，**这些行为可能违反 Cursor 的服务条款**，可能导致账号被限制或封禁、资格被回收、本机 Cursor 异常。是否使用由你自行决定，风险与后果自负；作者不提供任何担保，也不承担任何责任。禁止用于商业用途、倒卖资格，或访问任何不属于你本人的账号与设备。

完整条款见 [DISCLAIMER.md](DISCLAIMER.md)。权利人如认为本仓库侵权，请提 Issue 或邮件联系，核实后会及时删除或下架。

## 项目结构

```
Cusor-bot-sand/
├─ app.py                # pywebview 入口 + JS 桥接
├─ web/                  # 前端 UI（index.html / style.css / app.js）
├─ accounts.py           # token/JSON 导入、去重、分组与持久化
├─ sand_api.py           # Sand 资格查询/领取（cursor.com 会话接口）
├─ account_usage.py      # 账期额度与按模型花费（api2 protobuf）
├─ local_cursor.py       # 读写本机 Cursor 登录态（探测 / 切号）
├─ browser_login.py      # CDP 注入 cookie，打开已登录浏览器
├─ sand_patch.py         # 本机 Cursor 客户端模式补丁 / 回退
├─ grok_box.py           # Grok Bot Box 网关：EnsureSandBox、relay 描述符、Box 路由 provision
├─ sand_rpc/             # InferenceService/Stream 的 Connect 客户端
├─ dns_fix.py            # hosts 修复（Clash fake-ip 等劫持）
├─ resolve.py            # DoH 绕过 DNS 劫持
├─ elevate.py            # UAC 提权拉起补丁子进程
├─ patch_install.bat     # 补丁命令行入口（另有 patch_restore / patch_status）
├─ make_icon.py          # 图标 → 多尺寸 icon.ico
├─ patch_plugin.py       # 修补 Nuitka pywebview 插件（补 win32）
├─ installer.iss         # Inno Setup 安装包脚本
├─ ChineseSimplified.isl # 安装向导简体中文语言包
├─ build.bat             # 一键：编译 + 打安装包
├─ build_win.bat         # 仅编译（release / fast / deps）
├─ codemagic.yaml        # macOS 打 .app / .dmg 的 CI 流水线
├─ icon.ico              # 应用图标
└─ requirements.txt
```
