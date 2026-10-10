# omp-a2a-bridge-mcp

把 **oh-my-pi（omp）编码智能体** 暴露成 MCP 工具的桥接服务器。安装一次，任何 MCP 客户端（包括 omp 自己、Claude Code、Cline 等）都能直接调用 omp 干活。

核心来自 `omp-a2a-bridge`（A2A v1.0 桥），这里以 **stdio MCP server** 形式重新打包：
- 每次工具调用 → 一条 prompt 发给一个长驻的 `omp --mode rpc` 子进程
- 同一个 `context_id` 复用同一个子进程 → 多轮对话记忆保留
- 纯 Python 标准库，**零第三方依赖**

```
MCP client (omp / Claude Code / Cline / ...)
        │  stdio JSON-RPC
        ▼
src/mcp_server.py ──► src/omp_bridge_core.py ──► omp --mode rpc (长驻子进程)
```

## ✨ 功能

| 工具 | 说明 |
|---|---|
| `run_task` | 发送 prompt 给 omp，同步返回回复。支持代码读写/重构、构建、测试、调试等工程任务 |

参数：
- `prompt`（必填）：要 omp 执行的指令
- `context_id`（可选）：会话 ID。**同一个 ID 复用同一子进程**，多轮对话有记忆；不传则每次全新

## 📦 安装

### 方式一：作为 omp 插件（推荐）

```bash
omp plugin install @bearchild/omp-a2a-bridge-mcp
```

装完 omp 会自动根据包内的 `mcp.json` 拉起 MCP server。然后：

```text
/mcp reload
/mcp test omp-a2a-bridge-mcp   # 验证连接
```

### 方式二：直接跑（任何 MCP 客户端通用）

```bash
git clone https://github.com/你/omp-a2a-bridge-mcp.git
cd omp-a2a-bridge-mcp
python3 src/mcp_server.py          # stdio 模式
# 或在任意 MCP 客户端配置里注册：
#   command: python3
#   args: ["/path/to/omp-a2a-bridge-mcp/src/mcp_server.py"]
```

### 方式三：A2A 模式（原版 HTTP 服务）

仓库里保留的 `src/omp_bridge_core.py` 自带 A2A v1.0 HTTP server（`python3 src/omp_bridge_core.py`），监听 `127.0.0.1:9911`，供 A2A 客户端（如 Hermes 的 a2a_call）调用。MCP 模式不需要它。

## 🖥️ 平台支持（重要）

**不需要 WSL。** omp 官方支持 Windows 原生（win32-x64 预编译二进制，无需 WSL 桥接）。

用户机器需要满足：

| 依赖 | 说明 |
|---|---|
| `omp` | 必须已安装（`omp --version` 能跑） |
| `python3` | 必须（≥ 3.8 即可），bridge 是纯 Python 标准库 |

- **Linux / macOS**：直接装，默认 `OMP_BIN=omp`（自动在 PATH 查找）
- **Windows**：装 Python（python.org 官方安装包勾选 Add to PATH），`OMP_BIN` 自动探测 `omp.exe`；如果没进 PATH，设环境变量 `OMP_BIN=C:\path\to\omp.exe`
- **WSL**：也可以用（WSL 自带 python3），但**非必需**——原生 Windows 就能跑

## ⚙️ 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `OMP_BIN` | `omp`（自动探测） | omp 可执行文件路径 |
| `OMP_A2A_CWD` | `/root` | omp 工作目录（在哪干活） |
| `OMP_A2A_IDLE_TTL` | `1800` | 空闲回收秒数（超时杀掉闲置子进程） |
| `OMP_A2A_PROMPT_TIMEOUT` | `600` | 单次 prompt 超时秒数 |
| `OMP_A2A_LOG` | `INFO` | 日志级别 |

## 🔒 安全说明

- 桥接以 **yolo 审批模式**（`--approval-mode yolo`）启动 omp —— 调用方不需要逐条确认，**工具的执行权限等同运行 omp 的账号**
- `OMP_A2A_CWD` 限定工作目录；MCP 模式下进程由客户端本机拉起，调用方就是本机用户
- A2A HTTP 模式可用 `OMP_A2A_TOKEN` 设置 Bearer token 鉴权（MCP stdio 模式不需要）

## 🧪 自测

```bash
# 1) 握手 + tools/list
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | python3 src/mcp_server.py

# 2) 真实调用
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"run_task","arguments":{"prompt":"回复：测试OK","context_id":"t1"}}}\n' | python3 src/mcp_server.py
```

## 📄 License

MIT
