function run_interference_simulator()
% 干扰模拟器 — 真实 USRP 模式入口

matlabRoot = fileparts(fileparts(mfilename('fullpath')));
addpath(matlabRoot);
project_setup();
cfg = interference_config();

[state.radio_tx, state.radio_rx] = radio_init(cfg);
state.ui_client = ui_bridge_init(cfg.ui_host, cfg.ui_port, cfg.ui_timeout);
state.cmd_client = cmd_bridge_init(cfg.cmd_host, cfg.cmd_port, cfg.cmd_timeout);

state.isStop = false;
state.seq = 0;
state.tx_state = "running";
state.prev_action.channel_idx = 0;
state.prev_action.power_db = 0;
state.prev_action.bw_mhz = 4;
state.prev_action.waveform_mode = 0;
state.last_signal = [];

pause(0.5);

disp("干扰模拟器已启动（真实 USRP 模式），按 Ctrl+C 停止。");
interference_simulator_loop(state, cfg);
end
