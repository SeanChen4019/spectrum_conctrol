function [radio_tx, radio_rx] = radio_init(cfg)
% 单台 USRP 时分收发版
% 同一台 X310：
%   - RX 先收一帧做环境感知
%   - TX 再发一帧干扰
% 这是当前最稳妥的单机 jammer 原型方式

radio_tx = comm.SDRuTransmitter( ...
    'Platform', 'X310', ...
    'IPAddress', cfg.ip_addr);

radio_tx.CenterFrequency = cfg.center_freq;
radio_tx.Gain = cfg.tx_gain_base;
radio_tx.MasterClockRate = cfg.MasterClockRate;
radio_tx.InterpolationFactor = cfg.InterpolationFactor;
radio_tx.ClockSource = 'Internal';
radio_tx.ChannelMapping = [1];
if isprop(radio_tx, 'Antenna')
    radio_tx.Antenna = 'TX/RX';
end

radio_rx = comm.SDRuReceiver( ...
    'Platform', 'X310', ...
    'IPAddress', cfg.ip_addr, ...
    'OutputDataType', 'double', ...
    'MasterClockRate', cfg.MasterClockRate, ...
    'DecimationFactor', cfg.InterpolationFactor, ...
    'SamplesPerFrame', cfg.sample_num);

radio_rx.CenterFrequency = cfg.center_freq;
radio_rx.Gain = cfg.rx_gain;
radio_rx.ChannelMapping = [1];
radio_rx.ClockSource = 'Internal';
if isprop(radio_rx, 'Antenna')
    radio_rx.Antenna = 'TX/RX';
end

disp("USRP 初始化完成（单 USRP 时分收发模式，RF A TX/RX 单天线口）。");
end
