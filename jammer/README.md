# 干扰模拟控制系统

面向应急保障场景的干扰模拟器，用于模拟极端环境下的无线干扰情况。

系统支持通过**智能体（Agent）**以文字指令方式控制干扰参数：切频、切换干扰模式、调整功率、查询频谱信息。

## 系统架构

```
┌─────────────────────────────┐          ┌──────────────────────────────┐
│  干扰模拟器 (MATLAB后端)      │          │  干扰控制台 (Python UI)        │
│                              │telemetry │                               │
│  - USRP 频谱感知              │◄────────►│  - 实时频谱/信道监控            │
│  - 干扰波形生成               │  commands│  - 智能体控制面板               │
│  - Mock模式(无USRP)          │          │  - 文字指令 → 切频/模式/功率    │
└─────────────────────────────┘          └──────────────────────────────┘
```

- **MATLAB 后端**：连接 USRP 进行频谱感知和干扰波形发射，或使用 Mock 模式模拟
- **Python UI**：PyQt5 实时监控面板 + AI 智能体对话控制

## 目录结构

```text
├── README.md
├── PROTOCOL.md
├── matlab/
│   ├── project_setup.m
│   ├── backend/
│   │   ├── interference_config.m          # 干扰模拟器配置
│   │   ├── run_interference_simulator.m    # 真实USRP模式入口
│   │   ├── interference_simulator_loop.m   # 主循环
│   │   └── run_mock_interference_simulator.m  # Mock模式入口
│   ├── interface/
│   │   ├── cmd_bridge_init.m
│   │   ├── cmd_bridge_poll.m
│   │   ├── ui_bridge_init.m
│   │   └── ui_send_telemetry.m
│   ├── radio/
│   │   └── radio_init.m
│   ├── sensing/
│   │   └── sense_channel_grid.m
│   └── waveform/
│       └── generate_jammer_signal.m
└── python/
    ├── app/
    │   ├── requirements.txt
    │   ├── main_window.py                 # UI 主窗口
    │   ├── telemetry_server.py            # 遥测接收服务
    │   ├── command_bridge.py              # 命令桥接
    │   └── agent/
    │       ├── agent_brain.py             # Agent 编排
    │       ├── agent_panel.py             # 对话面板
    │       ├── llm_client.py              # LLM 客户端
    │       ├── strategy_engine.py         # 命令解析器
    │       └── tools.py                   # 工具定义
    └── rl_service/                        # (历史参考) RL决策服务
```

## 快速开始

### 1. 启动 UI 控制台

```bash
conda activate jammer_ui
cd python/app
pip install -r requirements.txt
python main_window.py
```

### 2. 启动干扰模拟器

**Mock 模式（无 USRP，推荐先跑）：**
```matlab
run('matlab/project_setup.m');
run_mock_interference_simulator
```

**真实 USRP 模式：**
```matlab
cd('C:\Users\503\Desktop\jammer_competition\matlab')
run('project_setup.m')
run_interference_simulator
```
## 智能体控制

在 UI 右侧面板输入文字指令，智能体会自动解析并执行：

| 指令示例 | 功能 |
|---------|------|
| "切换到信道5" | 切频到信道5 |
| "切换为宽带噪声干扰模式" | 切换为噪声模式 |
| "切换为多音干扰模式" | 切换为多音模式 |
| "功率增加到15dB" | 调整功率 |
| "分析当前频谱态势" | 查询频谱状态 |
| "查询系统配置和运行状态" | 查询系统信息 |

也可以直接点击快捷命令按钮。

## 干扰参数

| 参数 | 范围 | 说明 |
|------|------|------|
| 目标信道 | 0-9 (对应信道1-10) | 3GHz 附近10个信道 |
| 发射功率 | 0-20 dB | 步进5dB |
| 干扰带宽 | 2/4/6/8/20 MHz | 可选带宽 |
| 干扰模式 | 0=宽带噪声, 1=多音 | 两种干扰波形 |

## 通信协议

- **遥测端口**：TCP 5555 — MATLAB → UI，JSON-line格式
- **命令端口**：TCP 5557 — UI → MATLAB，poll/response模式

详见 [PROTOCOL.md](PROTOCOL.md)
