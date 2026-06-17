# 应急保障频谱智能管控仿真套件

这个文件夹只包含新增仿真文件，不修改现有发射端、接收端、干扰端 UI。

## 1. 生成录频时间线

```powershell
python .\simulation_suite\src\generate_jammer_data.py --scenario all
```

输出在 `simulation_suite/outputs/timelines/`，每个场景一个 `jsonl`。

## 2. 驱动三个现有 UI 录频

先分别启动现有 UI：

- `jammer/python/app/main_window.py`
- `transmitter_and_receive/发射机-http/ui_trans_pyqt5.py`
- `transmitter_and_receive/接收机-http/Copy_of_ui_pyqt5-0414.py`

然后运行：

```powershell
python .\simulation_suite\src\demo_ui_driver.py --scenario all
```

可先用 `--dry-run` 检查时间线，不向 UI 发送数据。

## 2.1 Jammer UI 本地固定序列录频

如果只录干扰机 UI，可以使用预生成的固定收发端序列。该序列已经保存到：

```text
simulation_suite/outputs/fixed_link_sequence.json
```

重新生成：

```powershell
python .\simulation_suite\src\generate_fixed_link_sequence.py
```

启动 `jammer/python/app/main_window.py` 后，UI 会自动读取该文件并持续刷新实时频谱。录频时依次点击右侧三个场景按钮即可：

- 启动默认：随机背景电磁环境，不属于三个演示场景。
- 基站退服：8MHz 干扰覆盖信道 1-4，固定收发链路避让到信道 9。
- 救援拥塞：4MHz 干扰覆盖信道 5-6，固定收发链路避让到信道 2。
- 链路压测：干扰覆盖信道 7-9，固定收发链路避让到信道 4。

每次点击场景按钮后，固定序列会直接显示已完成避让后的频谱态势；右侧 Agent 区只显示干扰侧参数对应的应急场景说明。

如需指定其他固定序列文件，可设置环境变量 `SPECTRUM_FIXED_SEQUENCE`。

## 3. 生成论文指标

```powershell
python .\simulation_suite\src\run_metrics.py
```

输出：

- `outputs/metrics/metrics_summary.csv`
- `outputs/metrics/ablation_table.md`
- `outputs/metrics/scenario_curves.png`
- `outputs/metrics/metrics_runs.csv`

## 4. 场景说明

三套场景围绕应急保障：

- 基站退服：宽带噪声抬升，优先保障图片/文本低速可靠回传。
- 救援拥塞：多音频点冲突，突出智能切频避让。
- 无人机压测：高功率宽带压制，突出视频链路恢复和关键帧降级。
