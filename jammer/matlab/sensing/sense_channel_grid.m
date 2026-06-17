function [channel_features, snr_est, bw_est, sig_type, channel_map] = sense_channel_grid(rx_signal, cfg, prev_action)
% 将当前接收频谱按 10 个信道切片，生成每个信道的特征
% 特征：occupancy, interference, utility, snr_like

N = max(4096, length(rx_signal));
rx_signal = rx_signal(:);
rx_signal = rx_signal - mean(rx_signal);  % 去除零中频直流偏置
spec = fftshift(fft(rx_signal, N));
power_spec = abs(spec).^2;
power_spec = suppress_center_dc(power_spec, cfg);
power_norm = power_spec / (max(power_spec) + 1e-12);

% 全局粗指标（兼容 UI）
signal_region = power_norm > 0.3;
noise_region = power_norm < 0.3;
if sum(signal_region) < 10
    signal_power = mean(power_spec);
else
    signal_power = mean(power_spec(signal_region));
end
if sum(noise_region) < 10
    noise_power = mean(power_spec);
else
    noise_power = mean(power_spec(noise_region));
end
snr_est = 10 * log10(signal_power / (noise_power + 1e-12));

active_bins = find(power_norm > 0.1);
if isempty(active_bins)
    bw_est = 0;
    sig_type = 0;
else
    bw_bins = max(active_bins) - min(active_bins);
    bw_est = bw_bins * (cfg.sample_rate / N);
    if bw_est < 20e6
        sig_type = 1;
    elseif bw_est < 50e6
        sig_type = 2;
    else
        sig_type = 3;
    end
end

freq_axis = linspace(cfg.center_freq - cfg.sample_rate/2, cfg.center_freq + cfg.sample_rate/2, N);
channel_features = zeros(cfg.num_channels, 4);
channel_map = zeros(1, cfg.num_channels);

for k = 1:cfg.num_channels
    ch_center = cfg.channel_centers_hz(k);
    ch_half = cfg.channel_width_hz / 2;
    idx = find(freq_axis >= (ch_center - ch_half) & freq_axis < (ch_center + ch_half));
    idx = remove_center_guard_bins(idx, freq_axis, cfg);
    if isempty(idx)
        continue;
    end

    ch_energy_raw = mean(power_spec(idx));
    ch_peak_raw = max(power_spec(idx));
    noise_floor = median(power_spec) + 1e-12;
    ch_snr_db = 10 * log10(ch_energy_raw / noise_floor);
    ch_peak_db = 10 * log10(ch_peak_raw / noise_floor);

    occ = max(0, min(1, ch_snr_db / 18));
    interf = max(0, min(1, (ch_peak_db - 12) / 18));
    util = max(0, min(1, 0.65 * occ + 0.35 * max(0, ch_peak_db / 25) - 0.25 * interf));
    snr_like = max(0, min(1, ch_snr_db / 25));

    channel_features(k, :) = [occ, interf, util, snr_like];

    if ch_snr_db < 4
        channel_map(k) = 0;
    else
        channel_map(k) = 1;
    end
end

% 标注上一时隙已执行干扰的覆盖范围（用于 UI 更直观看 overlap）
if isfield(prev_action, 'channel_idx') && isfield(prev_action, 'power_db') && double(prev_action.power_db) > 0
    affected = affected_channel_indices(prev_action, cfg);
    for n = 1:length(affected)
        idx = affected(n);
        if channel_map(idx) == 1 || channel_map(idx) == 3
            channel_map(idx) = 3;  % 用户占用 + 干扰覆盖
        else
            channel_map(idx) = 2;  % 干扰覆盖
        end
    end
end
end

function y = suppress_center_dc(x, cfg)
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

function idx = remove_center_guard_bins(idx, freq_axis, cfg)
if ~isfield(cfg, 'dc_notch_hz') || cfg.dc_notch_hz <= 0
    return;
end
guard_half_hz = double(cfg.dc_notch_hz) / 2;
keep = abs(freq_axis(idx) - cfg.center_freq) > guard_half_hz;
idx = idx(keep);
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
