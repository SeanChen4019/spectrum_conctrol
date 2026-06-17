function [y, tx_gain] = generate_jammer_signal(action, cfg)
% 根据控制参数生成干扰模拟波形
% action:
%   channel_idx   - 目标信道 (0-9)
%   power_db      - 发射功率 (0-20 dB)
%   bw_mhz        - 干扰带宽 (2/4/6/8 MHz)
%   waveform_mode - 干扰模式 (0=宽带噪声, 1=多音)

N = cfg.sample_num;
fs = cfg.sample_rate;
t = (0:N-1)' / fs;

channel_idx = double(action.channel_idx) + 1;
channel_idx = min(max(channel_idx, 1), cfg.num_channels);

power_db = min(max(double(action.power_db), cfg.power_db_min), cfg.power_db_max);
bw_mhz = double(action.bw_mhz);
waveform_mode = double(action.waveform_mode);

f0 = cfg.channel_centers_hz(channel_idx) - cfg.center_freq;    % 基带偏移
bw_hz = bw_mhz * 1e6;

amp = 10^(power_db / 20);
tx_gain = min(max(cfg.tx_gain_base + power_db * 0.5, cfg.tx_gain_min), cfg.tx_gain_max);

if waveform_mode == 0
    % 带限噪声
    base_noise = randn(N,1) + 1i * randn(N,1);
    norm_cutoff = min(max((bw_hz/2) / (fs/2), 0.001), 0.95);
    b = fir1(128, norm_cutoff, 'low');
    shaped = filter(b, 1, base_noise);
    y = amp * shaped .* exp(1i * 2 * pi * f0 * t);
else
    % 多音干扰
    tone_count = 4;
    tone_spacing = bw_hz / max(tone_count, 1);
    tones = zeros(N,1);
    offsets = linspace(-bw_hz/2, bw_hz/2, tone_count);
    for k = 1:tone_count
        tones = tones + exp(1i * 2 * pi * (f0 + offsets(k)) * t);
    end
    y = amp * tones / sqrt(tone_count);
end

y = y / (max(abs(y)) + 1e-12);
end
