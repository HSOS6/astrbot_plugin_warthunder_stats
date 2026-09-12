# 战雷战绩查询 (astrbot_plugin_warthunder_stats)

一个用于查询《战争雷霆》(War Thunder) 玩家战绩的 AstrBot 插件。插件内置战雷助手 App 数据源，支持玩家战绩总览、联队信息等，并默认输出战绩卡片图片。注意：本插件不是开箱即用，请遵循文档进行部署

![logo](./logo.png)

喜欢就点个Star吧~

## 功能特性

- **战绩总览**：展示街机、历史、拟真三种模式的 PVP 数据。
- **App 排名数据**：展示战雷助手 App 接口返回的 PVP 评分和平均相对排名。
- **联队查询**：展示联队简介、成员列表和活跃度等信息。
- **图片渲染**：使用 Pillow 绘制包含头像、等级经验、各模式数据和各系载具统计的战绩卡片。
- **多数据源**：支持战雷助手 App 接口、官网页面和自定义 API（暂时没有相关API）。
- 此插件界面主要仿AXBOT制作，但是此插件使用的所有接口均是我从官方软件逆向而来，未使用任何第三方接口（虽然说我也没找到），当然也欢迎参考我的代码，或是提出issues

## 准备内容
1. 安装本插件
2. 一个战争雷霆账号（无论老旧，但是更建议使用新注册的小号防止账号被封禁）
3. 部署FlareSolverr（docker）
FlareSolverr Docker 部署示例：

```bash
docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```

> 使用的账号不要开启二次验证（如Gaijin Pass）！会导致无法登录！！

## 安装

1. 安装插件
2. 安装依赖（默认自动安装）：

```bash
pip install -r astrbot_plugin_warthunder_stats/requirements.txt
```

3. 日志中出现 `[WarThunderStats] 插件已加载` 表示插件加载成功。

## 配置教程（最简）
- 填写Gaijin账号邮箱及密码
- 填写 FlareSolverr 服务器地址（一般是 http://你的服务器IP:8191 ）
> 其他内容无需修改

## 配置说明

在 AstrBot 管理面板的插件配置页中设置。如果你不知道API是什么（目前没有相关API），请将 `data_source` 设为 `app`，并填写 Gaijin 账号和密码，以获取较完整的数据。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `data_source` | string | `app` | 数据源：`app`、`official` 或 `custom` |
| `app_login` | string | 空 | Gaijin 账号，仅 `app` 模式使用；建议使用专用账号 |
| `app_password` | string | 空 | Gaijin 密码，仅 `app` 模式使用；只会从本地提交至 Gaijin 官方接口 |
| `api_base_url` | string | 空 | 自定义 API 基础地址，仅 `custom` 模式使用，结尾不需要斜杠 |
| `api_key` | string | 空 | 自定义 API 密钥；接口不需要密钥时留空 |
| `api_timeout_seconds` | int | `20` | API 请求超时时间，单位为秒 |
| `default_mode` | string | `arcade` | 默认模式：`arcade`、`realistic` 或 `simulation` |
| `enable_clan_info` | bool | `true` | 查询战绩时是否附带联队信息；启用后会增加一次官网请求 |

### 官网数据源配置

`official` 数据源需要访问官网玩家信息页，该页面可能受到 Cloudflare Turnstile 拦截。可按环境选择以下方案：

| 配置项 | 说明 |
| --- | --- |
| `flaresolverr_url` | FlareSolverr 服务地址，例如 `http://localhost:8191` |
| `cf_clearance` | 浏览器中取得的 Cloudflare Cookie，可能与 IP 和 User-Agent 绑定 |
| `browser_user_agent` | 获取 `cf_clearance` 时浏览器使用的 User-Agent |
| `browser_impersonate` | `curl_cffi` 使用的浏览器指纹目标 |
| `nodriver_headless` | 使用 nodriver 时是否以无头模式启动浏览器 |

FlareSolverr Docker 部署示例：

```bash
docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```

## 指令列表

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/战雷战绩 <昵称> [模式]` | `wt战绩`、`战争雷霆战绩`、`战绩总览` | 查询玩家战绩，默认输出图片，渲染失败时回退为文本 |
| `/战雷战绩文本 <昵称> [模式]` | `wt战绩文本`、`战绩文本` | 查询玩家战绩并输出纯文本 |
| `/战雷联队 <联队名称>` | `wt联队`、`战雷战队`、`联队查询` | 查询联队信息，联队名称需要使用英文全名 |

模式参数支持：

- 街机：`街机`、`arc`、`arcade`
- 历史：`历史`、`rb`、`realistic`
- 拟真：`全真`、`sb`、`simulation`

不填写模式时使用 `default_mode` 配置。

### 使用示例

```text
/战雷战绩 HOSO6
/战雷战绩 HOSO6 历史
/战雷战绩文本 HOSO6
/战雷联队 Long live the People
```

## 数据源对比

| 数据源 | 优点 | 限制 |
| --- | --- | --- |
| `app`（推荐） | 不受官网 Cloudflare 影响；包含三模式战绩、相对排名和载具明细 | 需要 Gaijin 账号和密码 |
| `official` | 无需 Gaijin 账号；可查询官网玩家资料和联队详情 | 玩家信息页可能被 Cloudflare 拦截，需要额外配置绕过方案 |
| `custom` | 可接入自行部署的数据服务 | 必须配置 `api_base_url` 并实现插件约定的接口 |

## 依赖

- 基础依赖：`aiohttp`、`Pillow`、`httpx`、`cryptography`、`blackboxprotobuf`，详见 [requirements.txt](requirements.txt)。
- `official` 模式还需要 `beautifulsoup4`、`curl_cffi`，并根据环境配置 FlareSolverr、`cf_clearance` 或 `nodriver`。
- 插件内置约 2.7 MiB 的 Noto Sans SC Medium（500 字重）子集字体作为中文渲染兜底，覆盖 GB2312 常用汉字及插件现有称号、载具和界面文本。缺少字符时可安装完整的微软雅黑、Noto Sans CJK 或文泉驿字体。

## 常见问题

**查询提示被 Cloudflare 拦截怎么办？**

优先使用 `app` 数据源。必须使用 `official` 数据源时，建议部署 FlareSolverr，或配置有效的 `cf_clearance`。

**注册时间为什么显示 `[无数据]`？**

战雷助手 App 公共接口不返回注册时间。如果同时配置了可用的官网访问方案，插件会尝试从官网补充注册时间；补充失败不会影响 App 战绩查询。

**App 模式登录失败怎么办？**

检查账号和密码是否正确，以及账号登录是否需要额外验证。建议使用专门用于查询的账号。

**Custom 模式提示未配置 `api_base_url` 怎么办？**

在插件配置中填写自定义 API 的基础地址。插件不会在该配置缺失时回退到官网数据源。

## 其他文件

| 文件 | 说明 |
| --- | --- |
| [get_cf_cookie.py](get_cf_cookie.py) | 从 Chrome 或 Edge Cookie 数据库提取 `cf_clearance` 的工具 |
| [_title_map.json](_title_map.json) / [_titles.csv](_titles.csv) | 称号中文映射表 |
| [_units.csv](_units.csv) | 载具名称中文映射表 |
| [fonts/NotoSansSC-Medium-Subset.ttf](fonts/NotoSansSC-Medium-Subset.ttf) | 图片渲染使用的 Noto Sans SC Medium 子集字体 |
| [fonts/OFL.txt](fonts/OFL.txt) | 内置字体的 SIL Open Font License 1.1 许可证 |

## 项目信息

- 作者：星见雅
- 版本：v2.2.0
- 仓库：[HSOS6/astrbot_plugin_warthunder_stats](https://github.com/HSOS6/astrbot_plugin_warthunder_stats)
- 支持平台：aiocqhttp、qq_official，其他平台未经测试

数据来源于 War Thunder 官网与战雷助手 App 接口，仅供学习和交流使用，参考本项目代码请标明原作者及本项目地址。

有新的内容和点子欢迎提交PR和issues！