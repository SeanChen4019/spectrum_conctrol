# 通信协议说明

## 1. MATLAB -> UI：telemetry

```json
{
  "type": "telemetry",
  "version": "2.0",
  "session_id": "interference_simulator_001",
  "seq": 12,
  "timestamp_ms": 1710000000012,
  "telemetry": {
    "snr_est": 10.2,
    "bw_est": 25000.0,
    "sig_type": 1,
    "tx_state": "running",
    "spectrum": [...],
    "freq_axis_ghz": [...],
    "jam_waveform_abs": [...],
    "channel_map": [0,1,3,0,2,0,0,1,0,0],
    "channel_features": [
      [0.91, 0.22, 0.68, 0.83]
    ]
  },
  "action": {
    "channel_idx": 6,
    "power_db": 12.5,
    "bw_mhz": 4,
    "waveform_mode": 1
  },
  "rl_meta": {
    "policy": "manual",
    "value": 0,
    "latency_ms": 0
  }
}
```

### 字段说明

- `telemetry.spectrum`: 频谱幅度数组（降采样至512点）
- `telemetry.freq_axis_ghz`: 对应频率轴（GHz）
- `telemetry.jam_waveform_abs`: 干扰波形幅度
- `telemetry.channel_map`: 每信道状态，0=空闲 1=用户占用 2=被干扰 3=重叠
- `telemetry.channel_features[k]`: 4维特征 `[occupancy, interference, utility, snr_like]`
- `action`: 当前执行的干扰参数
- `rl_meta.policy`: 固定为 `"manual"`（手动控制模式）

## 2. UI -> MATLAB：命令桥接 (cmd poll/response)

MATLAB 定期轮询：

```json
{"type": "cmd_poll"}
```

UI 返回待执行命令：

```json
{
  "type": "cmd_rsp",
  "commands": [
    {"channel_idx": 3, "power_db": 15.0},
    {"waveform_mode": 1}
  ],
  "timestamp_ms": 1710000000050
}
```

### 支持的命令字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `channel_idx` | int | 目标信道 0-9 |
| `power_db` | float | 发射功率 0-20 dB |
| `bw_mhz` | int | 干扰带宽 2/4/6/8 MHz |
| `waveform_mode` | int | 干扰模式 0=宽带噪声 1=多音 |

当 `commands` 为空数组时表示无待处理命令。
