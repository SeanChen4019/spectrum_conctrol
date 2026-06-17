function cfg = interference_config()
% 干扰模拟器配置
% 面向应急保障场景：模拟极端干扰情况
% USRP 感知 + 干扰波形生成 + 遥测推送
% 所有干扰参数由 UI 智能体通过命令桥接下发

cfg.session_id = "interference_simulator_001";

cfg.center_freq = 3.0e9;

% ---------- 频谱观测与信道划分 ----------
cfg.total_span_hz = 20e6;
cfg.num_channels = 10;
cfg.channel_width_hz = 2e6;
cfg.channel_centers_hz = linspace(cfg.center_freq - 9e6, cfg.center_freq + 9e6, cfg.num_channels);

% ---------- 干扰参数 ----------
cfg.bw_choices_mhz = [2, 4, 6, 8];
cfg.power_levels_db = [0, 5, 10, 15, 20];
cfg.power_db_min = 0;
cfg.power_db_max = 20;
cfg.waveform_modes = [0, 1];  % 0=宽带噪声, 1=多音

% ---------- USRP 配置 ----------
cfg.ip_addr = '192.168.10.2';
cfg.MasterClockRate = 200e6;
cfg.InterpolationFactor = 10;
cfg.sample_rate = cfg.MasterClockRate / cfg.InterpolationFactor;
cfg.sample_num = 4096;
cfg.dc_notch_bins = 7;
cfg.dc_notch_hz = 1.0e6;
cfg.tx_gain_base = 5;
cfg.tx_gain_min = 0;
cfg.tx_gain_max = 25;
cfg.rx_gain = 15;
cfg.tx_repeat = 1;
cfg.pause_sec = 0.08;

% ---------- UI 遥测 ----------
cfg.ui_host = "127.0.0.1";
cfg.ui_port = 5555;
cfg.ui_timeout = 1;

% ---------- 命令桥接 (Python UI -> MATLAB) ----------
cfg.cmd_host = "127.0.0.1";
cfg.cmd_port = 5557;
cfg.cmd_timeout = 1;
end
