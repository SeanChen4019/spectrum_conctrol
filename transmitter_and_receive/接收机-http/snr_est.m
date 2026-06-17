function y = snr_est(rx_sig, mode)
% 判决导向信噪比估计（适配 QPSK 和 BPSK）
% mode: 0=常规QPSK, 1=BPSK+扩频, 2=切频QPSK
if nargin < 2
    mode = 0;
end

if mode == 1
    % BPSK: 只有 I 路承载信息，Q 路为噪声
    mean_i = mean(abs(real(rx_sig)));
    ideal = sign(real(rx_sig)) * mean_i;  % 实向量，虚部隐式为 0
else
    % QPSK: I/Q 独立判决
    mean_re = mean(abs(real(rx_sig)));
    mean_im = mean(abs(imag(rx_sig)));
    ideal = sign(real(rx_sig)) * mean_re + 1i * sign(imag(rx_sig)) * mean_im;
end

signal_power = mean(abs(ideal).^2);
noise_power = mean(abs(rx_sig - ideal).^2);

y = signal_power / max(noise_power, 1e-6);
end
