function run_mock_interference_simulator()
% 干扰模拟器 — Mock 模式（无 USRP）
% 模拟 10 信道频谱 + 干扰波形，验证 UI 遥测和命令链路

matlabRoot = fileparts(fileparts(mfilename('fullpath')));
addpath(matlabRoot);
project_setup();
cfg = interference_config();

ui_client = ui_bridge_init(cfg.ui_host, cfg.ui_port, cfg.ui_timeout);
cmd_client = cmd_bridge_init(cfg.cmd_host, cfg.cmd_port, cfg.cmd_timeout);

session_id = "mock_interference_001";
seq = 0;

prev_action.channel_idx = 0;
prev_action.power_db = 0;
prev_action.bw_mhz = 4;
prev_action.waveform_mode = 0;

disp("干扰模拟器 Mock 模式已启动，按 Ctrl+C 停止。");

while true
    seq = seq + 1;

    try
        cmds = cmd_bridge_poll(cmd_client);
        for i = 1:length(cmds)
            cmd = cmds{i};
            if isfield(cmd, 'channel_idx')
                prev_action.channel_idx = double(cmd.channel_idx);
                fprintf('[CMD] 切换信道 -> %d\n', prev_action.channel_idx + 1);
            end
            if isfield(cmd, 'power_db')
                prev_action.power_db = max(0, min(20, double(cmd.power_db)));
                fprintf('[CMD] 调整功率 -> %.1f dB\n', prev_action.power_db);
            end
            if isfield(cmd, 'bw_mhz')
                val = double(cmd.bw_mhz);
                if ismember(val, [2, 4, 6, 8])
                    prev_action.bw_mhz = val;
                    fprintf('[CMD] 调整带宽 -> %d MHz\n', val);
                end
            end
            if isfield(cmd, 'waveform_mode')
                val = double(cmd.waveform_mode);
                if val == 0 || val == 1
                    prev_action.waveform_mode = val;
                    fprintf('[CMD] 切换干扰模式 -> %d\n', val);
                end
            end
        end
    catch
    end

    [channel_features, channel_map] = build_mock_channel_features(cfg, prev_action);
    snr_est = 5 + 15 * rand();
    bw_est = 10e6 + 10e6 * rand();
    sig_type = 1;
    tx_state = "mock_running";

    action = prev_action;
    rl_meta.policy = "manual";
    rl_meta.value = 0;
    rl_meta.latency_ms = 0;

    prev_action = action;

    rx_signal = build_mock_rx_signal(cfg, channel_features);
    [jam_sig, ~] = generate_jammer_signal(action, cfg);

    ui_send_telemetry( ...
        ui_client, session_id, seq, cfg.center_freq, ...
        rx_signal, jam_sig, snr_est, bw_est, sig_type, tx_state, ...
        action, rl_meta, channel_map, channel_features, cfg);

    fprintf('[MOCK][SEQ=%d] ch=%d, power=%.1f dB, bw=%d MHz, mode=%d\n', ...
        seq, action.channel_idx, action.power_db, action.bw_mhz, ...
        action.waveform_mode);

    pause(0.12);
end
end

function [features, channel_map] = build_mock_channel_features(cfg, prev_action)
features = zeros(cfg.num_channels, 4);
channel_map = zeros(1, cfg.num_channels);
for k = 1:cfg.num_channels
    occ = rand();
    interf = rand();
    util = 0.2 + 0.8 * rand();
    snr_like = max(0, min(1, occ * (1 - interf) + 0.1 * rand()));
    features(k, :) = [occ, interf, util, snr_like];
    if occ < 0.25
        channel_map(k) = 0;
    elseif interf > 0.65
        channel_map(k) = 2;
    else
        channel_map(k) = 1;
    end
end
if isfield(prev_action, 'power_db') && double(prev_action.power_db) > 0
    affected = affected_channel_indices(prev_action, cfg);
    for n = 1:length(affected)
        idx = affected(n);
        if channel_map(idx) == 1 || channel_map(idx) == 3
            channel_map(idx) = 3;
        else
            channel_map(idx) = 2;
        end
    end
end
end

function indices = affected_channel_indices(action, cfg)
center_idx = double(action.channel_idx) + 1;
center_idx = min(max(center_idx, 1), cfg.num_channels);
if isfield(action, 'bw_mhz')
    bw_hz = double(action.bw_mhz) * 1e6;
else
    bw_hz = cfg.channel_width_hz;
end
center_hz = cfg.channel_centers_hz(center_idx);
left_hz = center_hz - bw_hz / 2;
right_hz = center_hz + bw_hz / 2;
indices = [];
for k = 1:cfg.num_channels
    ch_left = cfg.channel_centers_hz(k) - cfg.channel_width_hz / 2;
    ch_right = cfg.channel_centers_hz(k) + cfg.channel_width_hz / 2;
    if ch_right > left_hz && ch_left < right_hz
        indices(end + 1) = k; %#ok<AGROW>
    end
end
if isempty(indices)
    indices = center_idx;
end
end

function rx_signal = build_mock_rx_signal(cfg, channel_features)
N = cfg.sample_num;
fs = cfg.sample_rate;
t = (0:N-1)' / fs;
rx_signal = 0.05 * (randn(N,1) + 1i * randn(N,1));
for k = 1:cfg.num_channels
    occ = channel_features(k,1);
    interf = channel_features(k,2);
    amp = 0.1 + 0.5 * occ * (1 - 0.5 * interf);
    f0 = cfg.channel_centers_hz(k) - cfg.center_freq;
    rx_signal = rx_signal + amp * exp(1i * 2 * pi * f0 * t);
end
end
