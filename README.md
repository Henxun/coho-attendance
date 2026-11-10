# coho-attendance

Coho WTS 考勤系统（`w.coho.net.cn`）的 **MCP server**。

接入 DeepSeek Harness、Claude Desktop、Cline、Cherry Studio 等 MCP 客户端后，可以直接在对话里查询自己的考勤，例如：

- “我 10 月的考勤怎么样？有没有迟到、缺卡？”
- “10 月 13 号我几点打的卡？排的什么班？”
- “对比一下 9 月和 10 月的出勤工时。”

server 会用你的账号登录考勤系统、拉取月度考勤日历，并把原始数据整理成月报、指标摘要或单日明细返回。账号、密码、租户码只从环境变量读取，不会作为工具参数出现在对话记录里。

## 提供的工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `get_current_user` | – | 当前登录账号信息（姓名、工号、租户、globalId），用来验证凭据是否有效 |
| `get_month_report` | `year`, `month` | Markdown 月报：关键指标 + 异常明细 + 每日打卡记录，日常最常用 |
| `get_month_summary` | `year`, `month` | 只含关键指标的紧凑 JSON，适合多个月份对比 |
| `get_month_calendar` | `year`, `month` | 接口原始数据：每日汇总、打卡、排班、月度合计的全部字段 |
| `get_day_detail` | `date`（`YYYY-MM-DD`） | 某一天的打卡时间、排班和当天所有非零统计项 |

工具总是返回文本：要么是数据，要么是一条可读的错误说明（凭据缺失、登录失败、日期格式错误等），不会把异常堆栈抛给客户端。

## 安装

需要 Python 3.10+。

```bash
git clone https://github.com/Henxun/coho-attendance.git
cd coho-attendance

python -m venv .venv
# Windows
.venv\Scripts\python.exe -m pip install -e .
# macOS / Linux
.venv/bin/python -m pip install -e .
```

## 配置凭据

复制模板并填入真实值（`.env` 已在 `.gitignore` 中，不会被提交）：

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

```dotenv
COHO_USERNAME=你的工号
COHO_PASSWORD=你的密码
COHO_TENANT_CODE=你的租户码
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `COHO_USERNAME` | ✅ | 登录账号（工号）。兼容 `COHO_USER`、`COHO_ACCOUNT` |
| `COHO_PASSWORD` | ✅ | 明文登录密码，程序内部自行哈希。兼容 `COHO_PASSWD`、`COHO_PWD` |
| `COHO_TENANT_CODE` | ✅ | 租户码，即登录页 URL 里的 `tenantCode`，如 `ATS`。兼容 `COHO_TENANT` |
| `COHO_BASE_URL` | ❌ | 服务地址，默认 `https://w.coho.net.cn` |
| `COHO_ENV_FILE` | ❌ | 指定 `.env` 文件路径 |

`.env` 的查找顺序：`COHO_ENV_FILE` → 当前工作目录 → 项目根目录。所以 MCP 客户端用任意工作目录启动 server 都能读到配置。

进程里已经存在的环境变量优先于 `.env`。如果在 MCP 客户端配置的 `env` 里也写了同名变量，以客户端配置为准。

## 接入 MCP 客户端

server 使用 stdio 传输，启动命令是 `<项目路径>/.venv/Scripts/python.exe -m coho_attendance`（macOS / Linux 为 `.venv/bin/python`）。下面的 `<项目路径>` 替换为实际的克隆目录。

### DeepSeek Harness

编辑 profile 目录下的 `cordis.patch.yml`（例如 `~/.dsh/profiles/desktop/cordis.patch.yml`），在顶层 `insert` 列表中加入：

```yaml
- insert:
    - id: mcp-coho-attendance-local
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: coho-attendance
        transport: stdio
        command: '<项目路径>/.venv/Scripts/python.exe'
        args:
          - '-m'
          - 'coho_attendance'
        cwd: '<项目路径>'
        env:
          PYTHONUTF8: '1'
          PYTHONIOENCODING: 'utf-8'
```

如果已有 `insert` 列表，只追加 `- id: mcp-coho-attendance-local` 这一项即可。改完后重启 DeepSeek Harness。

### Claude Desktop / 其他客户端

```json
{
  "mcpServers": {
    "coho-attendance": {
      "command": "<项目路径>/.venv/Scripts/python.exe",
      "args": ["-m", "coho_attendance"],
      "cwd": "<项目路径>"
    }
  }
}
```

凭据默认从 `.env` 读取，客户端配置里不必写密码；确实需要时，也可以加在 `env` 字段里。

## 项目结构

```
.
├── pyproject.toml
├── .env.example                  # 凭据模板
└── src/coho_attendance/
    ├── __main__.py               # python -m coho_attendance 入口
    ├── server.py                 # MCP server 与工具定义
    ├── client.py                 # 登录、token 缓存、考勤日历接口
    ├── summary.py                # 原始 JSON → 指标 / 日报 / Markdown 月报
    └── config.py                 # 环境变量与 .env 读取、校验
```

## 实现说明

- 用 `tls_client` 模拟 Chrome TLS 指纹发请求，并带上 `sso_tenant_code` cookie，否则 `/auth/**` 接口会被 301 到统一登录页，返回 HTML 而不是 JSON。
- token 按登录接口返回的 `expTime` 缓存，提前 120 秒视为过期并自动重新登录；token 刷新加了锁，多个工具并发调用时只会登录一次。
- 考勤日历接口（`/wtspro-attendance/attSummaryComfirm/attCalendar`）需要 `key` / `sign` 签名，由 token、毫秒时间戳、`globalId` 和请求数据计算，实现见 `client.py` 的 `monthly_calendar()`。
- 接口返回的 `summaryCountList`（月度合计）、`detailCountList`（按日明细）、`dateList`（每日工时）、`dkList`（打卡）、`shiftInfoList`（排班）在 `summary.py` 里对齐成“一天一行”的视图。

### 登录密码的处理

登录接口 `POST /gateway/auth/account/requestToken` 不直接传密码，而是传哈希后的 `credential`。网页前端的算法是：

```
credential = HASH(password + secretKey).toUpperCase()
```

- `secretKey` 和 `HASH` 来自租户配置接口 `/saas/tenant/getTenantBasicConfig` 返回的 `auth.secretKey` / `auth.encryptType`，支持 `sha1`（默认）、`sha256`、`md5`，未知值回落到 `sha1`。
- 当前已知的 ATS 租户配置为空，即 `secretKey = ""`、算法 `sha1`。

本项目目前固定发送 `SHA1(password)` 的小写十六进制，ATS 租户已实测可以登录。如果某个租户配了 `secretKey` 或改用其他算法，需要修改 `client.py` 里的 `_credential()`。

## 免责声明

本项目是个人工具，通过模拟网页请求访问考勤系统，仅用于查询**自己账号**的考勤数据。请遵守所在公司的信息安全规定。考勤系统接口变更后，本项目可能失效。
