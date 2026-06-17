function interference_simulator_loop(state, cfg)
% 干扰模拟器主循环
%   感知频谱 → 接收控制命令 → 生成干扰信号 → 发送遥测

while ~state.isStop
    try
        state.seq = state.seq + 1;

        % ── 轮询控制命令（来自 Python UI 智能体）──
        try
            cmds = cmd_bridge_poll(state.cmd_client);
            for i = 1:length(cmds)
                cmd = cmds{i};
                state = apply_command(state, cmd);
            end
        catch
        end

        rx_signal = state.radio_rx();

        [channel_features, snr_est, bw_est, sig_type, channel_map] = ...
            sense_channel_grid(rx_signal, cfg, state.prev_action);

        action = state.prev_action;
        rl_meta.policy = "manual";
        rl_meta.value = 0;
        rl_meta.latency_ms = 0;

        state.prev_action = action;

        [jam_sig, tx_gain] = generate_jammer_signal(action, cfg);
        state.last_signal = jam_sig;

        try
            state.radio_tx.Gain = tx_gain;
            for k = 1:cfg.tx_repeat
                state.radio_tx(jam_sig);
            end
            state.tx_state = "running";
        catch MEtx
            state.tx_state = "error";
            disp("TX warning:");
            disp(MEtx.message);
        end

        ui_send_telemetry( ...
            state.ui_client, cfg.session_id, state.seq, cfg.center_freq, ...
            rx_signal, jam_sig, snr_est, bw_est, sig_type, state.tx_state, ...
            action, rl_meta, channel_map, channel_features, cfg);

        fprintf('[SEQ=%d] ch=%d, power=%.1f dB, bw=%d MHz, mode=%d\n', ...
            state.seq, action.channel_idx, action.power_db, action.bw_mhz, ...
            action.waveform_mode);

        pause(cfg.pause_sec);

    catch ME
        disp("MATLAB 后端错误：");
        disp(ME.message);
        disp("跳过当前帧，继续运行...");
        continue;
    end
end
end

function state = apply_command(state, cmd)
    if isfield(cmd, 'channel_idx')
        val = double(cmd.channel_idx);
        state.prev_action.channel_idx = val;
        fprintf('[CMD] 切换信道 -> %d\n', val + 1);
    end
    if isfield(cmd, 'power_db')
        val = double(cmd.power_db);
        state.prev_action.power_db = max(0, min(20, val));
        fprintf('[CMD] 调整功率 -> %.1f dB\n', state.prev_action.power_db);
    end
    if isfield(cmd, 'bw_mhz')
        val = double(cmd.bw_mhz);
        if ismember(val, [2, 4, 6, 8, 20])
            state.prev_action.bw_mhz = val;
            fprintf('[CMD] 调整带宽 -> %d MHz\n', val);
        else
            fprintf('[CMD] 无效带宽值 %d MHz，忽略 (允许 2/4/6/8/20)\n', val);
        end
    end
    if isfield(cmd, 'waveform_mode')
        val = double(cmd.waveform_mode);
        if val == 0 || val == 1
            state.prev_action.waveform_mode = val;
            fprintf('[CMD] 切换干扰模式 -> %s\n', ...
                iif(val == 0, "宽带噪声", "多音"));
        end
    end
end

function s = iif(cond, t, f)
    if cond, s = t; else, s = f; end
end
