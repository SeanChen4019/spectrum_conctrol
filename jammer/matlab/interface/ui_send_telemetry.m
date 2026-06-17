function ui_send_telemetry(client, session_id, seq, fc, rx_signal, intersig, snr_est, bw_est, sig_type, tx_state, action, rl_meta, channel_map, channel_features, cfg)
% 发送遥测数据到 Python UI
% rl_meta.policy 固定为 "manual" (手动控制模式)
try
    N = length(rx_signal);
    fs = cfg.sample_rate;
    rx_signal = rx_signal(:) - mean(rx_signal(:));
    spec = abs(fftshift(fft(rx_signal)));
    spec = suppress_center_dc_display(spec, cfg);
    f = fc + (-N/2:N/2-1) * (fs / N);

    packet.type = "telemetry";
    packet.version = "2.0";
    packet.session_id = session_id;
    packet.seq = seq;
    packet.timestamp_ms = posixtime(datetime('now')) * 1000;

    ds = max(1, floor(N / 512));
    packet.telemetry.snr_est = double(snr_est);
    packet.telemetry.bw_est = double(bw_est);
    packet.telemetry.sig_type = double(sig_type);
    packet.telemetry.tx_state = tx_state;
    packet.telemetry.spectrum = double(spec(1:ds:end));
    packet.telemetry.freq_axis_ghz = double(f(1:ds:end) / 1e9);

    if ~isempty(intersig)
        w = abs(intersig(:));
        ds2 = max(1, floor(length(w) / 512));
        packet.telemetry.jam_waveform_abs = double(w(1:ds2:end)).';
    else
        packet.telemetry.jam_waveform_abs = [];
    end

    packet.telemetry.channel_map = double(channel_map);
    packet.telemetry.channel_features = double(channel_features);
    packet.action = action;
    packet.rl_meta = rl_meta;

    txt = jsonencode(packet);
    write(client, uint8([txt newline]), "uint8");
catch ME
    disp("遥测发送失败: " + ME.message);
end
end

function y = suppress_center_dc_display(x, cfg)
y = x;
half = center_notch_half_bins(length(y), cfg);
if half <= 0
    return;
end
n = length(y);
c = floor(n / 2) + 1;
lo = max(1, c - half);
hi = min(n, c + half);
ref_pad = max(20, half);
ref_lo = max(1, lo - 2 * ref_pad);
ref_hi = min(n, hi + 2 * ref_pad);
left_ref = ref_lo:(lo - 1);
right_ref = (hi + 1):ref_hi;
ref = y([left_ref, right_ref]);
if isempty(ref)
    fill_val = median(y);
else
    fill_val = median(ref);
end
y(lo:hi) = fill_val;
end

function half = center_notch_half_bins(n, cfg)
half = 0;
if isfield(cfg, 'dc_notch_bins') && cfg.dc_notch_bins > 0
    half = max(half, floor(double(cfg.dc_notch_bins) / 2));
end
if isfield(cfg, 'dc_notch_hz') && cfg.dc_notch_hz > 0 && isfield(cfg, 'sample_rate')
    hz_per_bin = double(cfg.sample_rate) / double(n);
    half = max(half, ceil(double(cfg.dc_notch_hz) / 2 / hz_per_bin));
end
end
