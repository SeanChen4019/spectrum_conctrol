% =========== 接收端：分块渐进式恢复 + 缺失填黑 + 预存块数据 ===========
clear
clc
close all force
warning('off', 'all');
clear functions;  % 清除持久变量，避免System object残留

fprintf('\n==================== 接收端（分块渐进式恢复）启动 ====================\n');

%% ================= 初始化 =================
if exist('radio_tx', 'var') && isvalid(radio_tx)
    release(radio_tx);
    clear radio_tx;
end
if exist('radio_rx', 'var') && isvalid(radio_rx)
    release(radio_rx);
    clear radio_rx;
end

samp_rate = 200e6 / 512;

% 频率偏移 (Low-IF): 与发射机匹配，将接收信号搬回 DC
% 发射机将信号搬到 +80 kHz，接收机搬回 0 Hz；LO 泄露被搬到 -80 kHz
FREQ_OFFSET = 130e3;  % 必须与发射机一致（推到奈奎斯特边缘，LO 落入信道0）

Threshold = 150;
threshold_manual = false;  % 用户手动设置阈值后跳过动态调整
BUS_RX_SAMPLES = 160000;
FB_TX_SAMPLES  = 60000;

IMAGE_GRID_ROWS = 8;
IMAGE_GRID_COLS = 8;
VIDEO_FRAME_NUM = 20;

RX_MODE = 1;  % 1=仅图像, 2=仅视频, 3=图像+视频, 4=文本（需与发射端一致）

STATE_COLLECT  = 1;
STATE_COMPLETE = 2;

Carrier_set = 2e9 : 0.5e9 : 4e9;
Power_gain_set = 0 : 1 : 30;

Anti_Jamming_Mode_bef = 0;
Carrier_select_bef = 3;
Trans_power_select_bef = 7;
Power_gain_select_bef = 15;

CenterFrequency = Carrier_set(Carrier_select_bef);

%% ================= 预处理：预存媒体块数据（按 RX_MODE） =================
script_dir = fileparts(mfilename('fullpath'));
if isempty(script_dir)
    script_dir = pwd;
end
addpath(script_dir);

mode_names = {'仅图像', '仅视频', '图像+视频', '文本'};
fprintf('[RX-INIT] 接收模式: %s\n', mode_names{RX_MODE});

has_image = ismember(RX_MODE, [1, 3]);
has_video = ismember(RX_MODE, [2, 3]);
has_text  = (RX_MODE == 4);

% 媒体文件路径：接收机本地目录（CRC校验需要与发射端相同的文件）
% 请从发射机PC复制 p2.jpg 和 视频.mp4 到本接收机目录
img_path = fullfile(script_dir, 'p2.jpg');
video_path = fullfile(script_dir, '视频.mp4');

% 预加载所有媒体类型（无论初始RX_MODE，避免动态模式切换时数据为空）
fprintf('[RX-INIT] 预存图片块数据...\n');
if exist(img_path, 'file')
    [img_blocks, img_crc] = preprocess_image(img_path, IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
    [info_h, info_w] = get_image_dims(img_path);
else
    img_blocks = {}; img_crc = zeros(IMAGE_GRID_ROWS, IMAGE_GRID_COLS, 'uint32');
    info_h = 240; info_w = 320;
end
img_received = false(IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
img_grid_data = cell(IMAGE_GRID_ROWS, IMAGE_GRID_COLS);

fprintf('[RX-INIT] 预存视频帧数据...\n');
if exist(video_path, 'file')
    [video_blocks_data, video_crc] = preprocess_video(video_path, VIDEO_FRAME_NUM);
else
    video_blocks_data = {}; video_crc = zeros(VIDEO_FRAME_NUM, 1, 'uint32');
end
video_received = false(VIDEO_FRAME_NUM, 1);
video_frame_data = cell(VIDEO_FRAME_NUM, 1);

text_total_pkts = 0;
text_pkt_received = [];
text_pkt_data = {};
if has_text
    fprintf('[RX-INIT] 文本接收模式\n');
end

img_total = IMAGE_GRID_ROWS * IMAGE_GRID_COLS;
vid_total = VIDEO_FRAME_NUM;
fprintf('[RX-INIT] 预存完成 | 图片 %d块 | 视频 %d帧\n', img_total, vid_total);

%% ================= 接收缓存 =================
rx_session_id = 0;
rx_total_pkt_num = 0;
rx_pkt_valid = false(1, 10000);

img_rebuild_done = false;
rebuild_status_txt = '等待接收图片分块...';
preview_image_path = '';

state = STATE_COLLECT;
trans_sigs_sample_num = FB_TX_SAMPLES;  % 反向链路帧长度
trans_sigs = complex(zeros(trans_sigs_sample_num, 1));
Data_Rec_signal = complex(zeros(BUS_RX_SAMPLES, 1));  % 复数初始化，匹配SDR IQ输出类型
Rec_sig_afr = 1;  % 初始化为标量，build_rx_ui_payload会跳过星座图

prev_img_recv = 0;
prev_vid_recv = 0;
prev_text_recv = 0;
dup_pkt_count = 0;
last_progress_idx = 0;
session_data_saved = false;
last_save_preview_path = '';

% 参数重复发送：变更后连续发送N次，防止信令链路丢帧
param_repeat_count = 0;
param_repeat_max = 3;

% 反馈链路健康监测
fb_tx_attempts = 0;   % 总发送次数
fb_tx_success = 0;    % 成功发送次数（无 underrun）
fb_success_rate = 0;  % 反馈链路成功率 (%)
fb_sig_power_threshold = 0.001;  % 低于此功率视为空闲，不计入健康统计

%% ================= UI 配置（接收端） =================
rx_ui.enable = true;
rx_ui.url = 'http://127.0.0.1:5000';
rx_ui.health_endpoint = '/api/health';
rx_ui.post_period = 2;   % UI更新频率
rx_ui.ctrl_period = 1;  % 每轮都检查决策更新，SDR硬件缓冲使得开销可忽略
rx_ui.timeout = 1.0;   % JSON大数据需要更长的超时时间
rx_ui = ui_init(rx_ui);
rx_ui.has_bg = true;  % 启用后台异步HTTP，避免webwrite阻塞SDR循环

%% ================= 预览图生成控制 =================
last_preview_gen = 0;  % 上次生成预览图的idx，避免每帧都编码JPEG

%% ================= SDR 初始化 =================
disp('正在初始化 USRP 硬件，请稍候...');

radio_tx = comm.SDRuTransmitter('Platform','X310','IPAddress','192.168.10.2');
radio_tx.ChannelMapping = 2;   % 子板2专用于信令链路发射（1.45GHz）
radio_tx.CenterFrequency = 1.45e9;
radio_tx.Gain = 15;  % 反向链路功率
radio_tx.MasterClockRate = 200e6;
radio_tx.InterpolationFactor = 512;
radio_tx.ClockSource = 'External';
radio_tx.UnderrunOutputPort = true;

radio_rx = comm.SDRuReceiver( ...
    'Platform','X310', ...
    'IPAddress','192.168.10.2', ...
    'OutputDataType','double', ...
    'MasterClockRate',200e6, ...
    'DecimationFactor',512, ...
    'SamplesPerFrame',BUS_RX_SAMPLES);
radio_rx.ClockSource = 'External';
radio_rx.ChannelMapping = 1;   % 子板1专用于数据链路接收
radio_rx.CenterFrequency = CenterFrequency;
radio_rx.Gain = 10;  % 降低增益，避免信号饱和
radio_rx.OverrunOutputPort = true;

% 注意: EnableDCCorrection/EnableIQCorrection 在当前MATLAB版本不可用
% 将在软件层面进行DC去除

% DC 陷波器初始化（脚本变量天然持久，无需 persistent）
dc_alpha = 0.999;       % IIR 跟踪系数，越接近 1 陷波越窄
dc_est = complex(0, 0);  % DC 估计值初始化为 0

cleanupObj = onCleanup(@() safe_release_rx(radio_tx, radio_rx));
disp('USRP 硬件初始化完成！');

%% ================= 主循环 =================
for idx = 1:100000
    % ---------- UI / 决策输入 (AI Agent) 非阻塞异步 ----------
    if rx_ui.enable && mod(idx, rx_ui.ctrl_period) == 0
        dec = ui_try_get_decision_async(rx_ui, idx, Carrier_select_bef, Anti_Jamming_Mode_bef, Power_gain_select_bef);
        if isfield(dec, 'needs_update') && dec.needs_update
            changed = false;

            if isfield(dec, 'anti_jamming_mode') && dec.anti_jamming_mode ~= Anti_Jamming_Mode_bef
                Anti_Jamming_Mode_bef = dec.anti_jamming_mode;
                changed = true;
            end
            if isfield(dec, 'carrier_select') && dec.carrier_select >= 1 && dec.carrier_select <= length(Carrier_set)
                if dec.carrier_select ~= Carrier_select_bef
                    Carrier_select_bef = dec.carrier_select;
                    CenterFrequency = Carrier_set(Carrier_select_bef);
                    radio_rx.CenterFrequency = CenterFrequency;
                    changed = true;
                end
            end
            if isfield(dec, 'power_gain_select') && dec.power_gain_select >= 1
                if dec.power_gain_select ~= Power_gain_select_bef
                    Power_gain_select_bef = dec.power_gain_select;
                    changed = true;
                end
            end
            if isfield(dec, 'trans_power_select') && dec.trans_power_select ~= Trans_power_select_bef
                Trans_power_select_bef = dec.trans_power_select;
                changed = true;
            end
            if isfield(dec, 'threshold') && dec.threshold ~= Threshold
                Threshold = dec.threshold;
                threshold_manual = true;  % 用户手动设置，跳过动态调整
                changed = true;
            end

            if changed
                param_repeat_count = param_repeat_max;  % 连续发送N次防丢帧
                src = '';
                desc = '';
                if isfield(dec, 'command_source'), src = dec.command_source; end
                if isfield(dec, 'command_description'), desc = dec.command_description; end
                fprintf('[RX-UI] [%s] %s | Carrier=%d | Gain=%d | Mode=%d | 重复%d次\n', ...
                    src, desc, Carrier_select_bef, Power_gain_select_bef, Anti_Jamming_Mode_bef, param_repeat_max);
            end
        end
    end

    % ---------- 反向链路：双子板每帧发送，无自干扰 ----------
    snr_val = snr_est(Data_Rec_signal, Anti_Jamming_Mode_bef);
    snr_db = 10 * log10(max(snr_val, 0.001));
    snr_byte = uint8(max(0, min(255, round(snr_db + 20))));
    fb_session = bitshift(uint16(Carrier_select_bef), 8) + uint16(snr_byte);
    % ack_base = [Trans_power(8bit) | Power_gain(8bit)]
    param_packed = bitshift(uint16(Trans_power_select_bef), 8) + uint16(Power_gain_select_bef);

    % --- 滑动窗口ACK编码: ack_bitmap = [window_start(16bit) | bitmap(16bit)] ---
    ack_win_size = 16;
    if state == STATE_COMPLETE && rx_total_pkt_num > 0
        % 全部收齐：发送 0xFFFFFFFF 作为"全部确认"信号，发射机收到后立即停止
        ack_bitmap_send = uint32(4294967295);  % 0xFFFFFFFF
    elseif rx_total_pkt_num > 0
        ack_win_total = max(1, ceil(rx_total_pkt_num / ack_win_size));
        ack_win_idx = mod(floor(idx / 2), ack_win_total);  % 每2帧切换窗口
        ack_win_start = ack_win_idx * ack_win_size;        % 0-indexed
        ack_bits = uint16(0);
        for b = 1:ack_win_size
            pkt_num = ack_win_start + b;
            if pkt_num <= length(rx_pkt_valid) && rx_pkt_valid(pkt_num)
                ack_bits = bitset(ack_bits, b);
            end
        end
        ack_bitmap_send = bitor(bitshift(uint32(ack_win_start), 16), uint32(ack_bits));
    else
        ack_bitmap_send = uint32(0);
    end

    % 参数变更后连续发送多次（参数值已嵌入反馈帧，发射机收到后自动比对应用）
    if param_repeat_count > 0
        param_repeat_count = param_repeat_count - 1;
        if mod(param_repeat_count, 5) == 0
            fprintf('[RX-FB] 参数重复发送中... 剩余%d次\n', param_repeat_count);
        end
    end

    Par_Trans_fb = Par_trans_sig_Gen('feedback', ...
        20, fb_session, param_packed, ack_bitmap_send, Anti_Jamming_Mode_bef);

    zero_pad_num_fb = trans_sigs_sample_num - length(Par_Trans_fb) - 2000;
    zero_pad_num_fb = max(zero_pad_num_fb, 0);
    trans_sigs = [zeros(zero_pad_num_fb,1); Par_Trans_fb; zeros(2000,1)];

    if mod(idx, 100) == 0
        fprintf('[RX-REV] 反向链路: Carrier=%d | Power=%d | Gain=%d | SNR≈%.1f dB | Mode=%d\n', ...
            Carrier_select_bef, Trans_power_select_bef, Power_gain_select_bef, snr_db, Anti_Jamming_Mode_bef);
    end

    % ---------- 硬件收发 ----------
    try
        [Data_Rec_signal, ~, rx_overrun] = radio_rx();

        % 频率偏移 (Low-IF): 发射机将信号搬到 +80 kHz，此处搬回 0 Hz
        % LO 泄露原在 0 Hz，经此搬移后到 -80 kHz，与信号完全分离
        t_rx = (0:length(Data_Rec_signal)-1)' / samp_rate;
        Data_Rec_signal = Data_Rec_signal .* exp(-1j * 2 * pi * FREQ_OFFSET * t_rx);

        % 软件层面DC去除：IIR一阶高通滤波器，消除残余直流偏置
        % 脚本变量天然持久，无需 persistent 关键字
        dc_est = dc_alpha * dc_est + (1 - dc_alpha) * mean(Data_Rec_signal);
        Data_Rec_signal = Data_Rec_signal - dc_est;

        tx_underrun = radio_tx(trans_sigs);

        % 反馈链路健康统计（仅在有信号时计入，避免空闲状态稀释指标）
        sig_power_now = mean(abs(Data_Rec_signal).^2);
        if sig_power_now > fb_sig_power_threshold
            fb_tx_attempts = fb_tx_attempts + 1;
            if ~tx_underrun
                fb_tx_success = fb_tx_success + 1;
            end
            if mod(idx, 200) == 0 && fb_tx_attempts > 0
                fb_success_rate = fb_tx_success / fb_tx_attempts * 100;
                fprintf('[RX-FB-HEALTH] 反馈链路: 成功率=%.1f%% (%d/%d)\n', ...
                    fb_success_rate, fb_tx_success, fb_tx_attempts);
            end
        elseif mod(idx, 200) == 0
            fprintf('[RX-FB-HEALTH] 反馈链路: 空闲中 (sig_power=%.4f)\n', sig_power_now);
        end

        if rx_overrun
            % 静默处理
        end

        if mod(idx, 20) == 0
            sig_fft_debug = fftshift(fft(Data_Rec_signal(1:2048)));
            % LO 泄露已被频率偏移搬到 -80 kHz，无需剔除 DC bin
            sig_amp_debug = 20 * log10(abs(sig_fft_debug) + 1e-10);
            fprintf('[RX-SIG] idx=%d | sig_power=%.4f | max_abs=%.4f | rx_gain=%d | freq=%.2f GHz | amp_range=[%.1f, %.1f] dB\n', ...
                idx, mean(abs(Data_Rec_signal).^2), max(abs(Data_Rec_signal)), radio_rx.Gain, CenterFrequency/1e9, ...
                min(sig_amp_debug), max(sig_amp_debug));
        end
    catch ME
        warning('[RX-ERR] 硬件异常：%s', ME.message);
        continue;
    end

    % ---------- 动态调门限 / 增益 ----------
    total_blocks = 0;
    if has_image, total_blocks = total_blocks + img_total; end
    if has_video, total_blocks = total_blocks + vid_total; end
    if has_text && text_total_pkts > 0, total_blocks = text_total_pkts; end
    recv_num = 0;
    if has_image, recv_num = recv_num + sum(img_received(:)); end
    if has_video, recv_num = recv_num + sum(video_received(:)); end
    if has_text, recv_num = sum(text_pkt_received(:)); end
    missing_num = total_blocks - recv_num;

    if ~threshold_manual
        if missing_num <= 8
            Threshold = 130;
        elseif missing_num <= 32
            Threshold = 150;
        else
            Threshold = 170;
        end
    end
    if missing_num <= 8
        radio_rx.Gain = 15;
    elseif missing_num <= 32
        radio_rx.Gain = 12;
    else
        radio_rx.Gain = 10;
    end

    % ---------- 数据接收解析 ----------
    [~, Rec_sig_afr, ~, ~, ~, ~, frame_packets] = Data_Rece_sig_Gen( ...
        Anti_Jamming_Mode_bef, Data_Rec_signal, 0, Threshold);
    pkt_num_this_round = length(frame_packets);

    if pkt_num_this_round > 0
        fprintf('[RX-DATA] 本循环解析到 %d 个数据包\n', pkt_num_this_round);
    end

    if pkt_num_this_round > 0
        for ii = 1:pkt_num_this_round
            pkt = frame_packets(ii);

            if rx_session_id == 0 || pkt.Session_ID ~= rx_session_id
                % --- 保存上一会话数据（新会话开始前） ---
                if rx_session_id ~= 0 && ~session_data_saved
                    prev_recv = 0; prev_total = 0;
                    if has_image
                        prev_total = prev_total + img_total;
                        prev_recv = prev_recv + sum(img_received(:));
                    end
                    if has_video
                        prev_total = prev_total + vid_total;
                        prev_recv = prev_recv + sum(video_received(:));
                    end
                    if has_text && text_total_pkts > 0
                        prev_total = prev_total + text_total_pkts;
                        prev_recv = prev_recv + sum(text_pkt_received(:));
                    end
                    if prev_recv > 0
                        fprintf('[RX-SAVE] 检测到新会话，保存上一会话数据 (%d/%d)...\n', prev_recv, prev_total);
                        [rebuild_status_txt, last_save_preview_path] = save_session_data( ...
                            script_dir, has_image, has_video, has_text, ...
                            img_grid_data, img_received, IMAGE_GRID_ROWS, IMAGE_GRID_COLS, info_h, info_w, ...
                            video_frame_data, video_received, VIDEO_FRAME_NUM, ...
                            text_pkt_received, text_pkt_data, text_total_pkts);
                    end
                end
                session_data_saved = false;

                rx_session_id = pkt.Session_ID;
                rx_total_pkt_num = pkt.Total_frame_num;
                rx_pkt_valid = false(1, max(10000, rx_total_pkt_num));

                % --- 根据信令链路中的 tx_mode 动态切换接收模式 ---
                if isfield(pkt, 'tx_mode') && pkt.tx_mode >= 1 && pkt.tx_mode <= 4
                    new_rx_mode = pkt.tx_mode;
                    if new_rx_mode ~= RX_MODE
                        fprintf('[RX-MODE] 信令链路触发模式切换: %d -> %d (%s -> %s)\n', ...
                            RX_MODE, new_rx_mode, mode_names{RX_MODE}, mode_names{new_rx_mode});
                        RX_MODE = new_rx_mode;
                        has_image = ismember(RX_MODE, [1, 3]);
                        has_video = ismember(RX_MODE, [2, 3]);
                        has_text  = (RX_MODE == 4);
                        % 通知 Python UI 任务模式已变更
                        if rx_ui.enable
                            try
                                sync_data = struct('rx_mode', RX_MODE, ...
                                                   'tx_mode_name', mode_names{RX_MODE});
                                sync_opts = weboptions('RequestMethod', 'post', ...
                                    'MediaType', 'application/json', 'Timeout', 3);
                                webwrite([rx_ui.url, '/api/task_sync'], sync_data, sync_opts);
                            catch
                            end
                        end
                    end
                end

                % 清除旧模式预览，避免模式切换后UI仍显示上一轮图片
                last_save_preview_path = '';
                preview_image_path = '';

                if has_image
                    img_received(:) = false;
                    img_grid_data = cell(IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
                end
                if has_video
                    video_received(:) = false;
                    video_frame_data = cell(VIDEO_FRAME_NUM, 1);
                end

                if has_text
                    text_total_pkts = 0;
                    text_pkt_received = [];
                    text_pkt_data = {};
                end

                img_rebuild_done = false;
                rebuild_status_txt = sprintf('新会话 session=%d，总块=%d，模式=%s', ...
                    rx_session_id, rx_total_pkt_num, mode_names{RX_MODE});

                state = STATE_COLLECT;
                threshold_manual = false;
                prev_img_recv = 0;
                prev_vid_recv = 0;
                prev_text_recv = 0;
                dup_pkt_count = 0;

                fprintf('[RX-SESSION] 新会话：session=%d | total=%d | mode=%s\n', ...
                    rx_session_id, rx_total_pkt_num, mode_names{RX_MODE});
            end

            if pkt.Frame_num >= 1 && pkt.Frame_num <= length(rx_pkt_valid)
                if ~rx_pkt_valid(pkt.Frame_num)

                    % --- 根据块元数据验证并填充 ---
                    blk_row = pkt.block_row;
                    blk_col = pkt.block_col;
                    blk_type = pkt.block_type;
                    blk_crc = pkt.block_crc32;

                    if blk_row >= 0 && blk_col >= 0
                        if blk_type == 0 && has_image
                            r = blk_row + 1;
                            c = blk_col + 1;
                            if r >= 1 && r <= IMAGE_GRID_ROWS && c >= 1 && c <= IMAGE_GRID_COLS
                                expected_crc = img_crc(r, c);
                                if blk_crc == expected_crc
                                    rx_pkt_valid(pkt.Frame_num) = true;
                                    img_received(r, c) = true;
                                    img_grid_data{r, c} = img_blocks{r, c};
                                else
                                    fprintf('[RX-WARN] 图片块(%d,%d) CRC mismatch, 等待重传\n', blk_row, blk_col);
                                end
                            end
                        elseif blk_type == 1 && has_video
                            f = blk_row + 1;
                            if f >= 1 && f <= VIDEO_FRAME_NUM
                                expected_crc = video_crc(f);
                                if blk_crc == expected_crc
                                    rx_pkt_valid(pkt.Frame_num) = true;
                                    video_received(f) = true;
                                    video_frame_data{f} = video_blocks_data{f};
                                else
                                    fprintf('[RX-WARN] 视频帧%d CRC mismatch, 等待重传\n', blk_row);
                                end
                            end
                        elseif blk_type == 2 && has_text
                            pkt_idx = blk_row + 1;
                            total_pkts = pkt.block_total_rows;
                            if text_total_pkts == 0
                                text_total_pkts = total_pkts;
                                text_pkt_received = false(1, total_pkts);
                                text_pkt_data = cell(1, total_pkts);
                                fprintf('[RX-TEXT] 检测到文本传输: %d 个包\n', total_pkts);
                            end
                            if pkt_idx >= 1 && pkt_idx <= length(text_pkt_received) && ~text_pkt_received(pkt_idx)
                                rx_pkt_valid(pkt.Frame_num) = true;
                                text_pkt_received(pkt_idx) = true;
                                text_pkt_data{pkt_idx} = pkt.Payload_bytes(10:end);
                                fprintf('[RX-TEXT] 收到文本包 %d/%d\n', pkt_idx, text_total_pkts);
                            end
                        end
                    end
                else
                    dup_pkt_count = dup_pkt_count + 1;
                end
            end
        end
    end

    % ---------- 统计与显示更新 ----------
    img_recv = 0; vid_recv = 0; text_recv = 0;
    if has_image, img_recv = sum(img_received(:)); end
    if has_video, vid_recv = sum(video_received(:)); end
    if has_text, text_recv = sum(text_pkt_received(:)); end
    total_recv = img_recv + vid_recv + text_recv;
    missing_num = total_blocks - total_recv;

    progress_happened = (img_recv > prev_img_recv) || (vid_recv > prev_vid_recv) || (text_recv > prev_text_recv);
    prev_img_recv = img_recv;
    prev_vid_recv = vid_recv;
    prev_text_recv = text_recv;

    if progress_happened
        last_progress_idx = idx;
        if has_text
            fprintf('[RX-STATE] 文本 %d/%d | dup=%d\n', text_recv, total_blocks, dup_pkt_count);
        elseif has_image && has_video
            fprintf('[RX-STATE] 图片 %d/%d | 视频 %d/%d | 总计 %d/%d | dup=%d\n', ...
                img_recv, img_total, vid_recv, vid_total, total_recv, total_blocks, dup_pkt_count);
        elseif has_image
            fprintf('[RX-STATE] 图片 %d/%d | dup=%d\n', img_recv, img_total, dup_pkt_count);
        else
            fprintf('[RX-STATE] 视频 %d/%d | dup=%d\n', vid_recv, vid_total, dup_pkt_count);
        end
    end

    % ---------- 完成判定 ----------
    if state == STATE_COLLECT && missing_num == 0 && total_blocks > 0
        state = STATE_COMPLETE;
        img_rebuild_done = true;

        if ~session_data_saved
            [save_status, last_save_preview_path] = save_session_data( ...
                script_dir, has_image, has_video, has_text, ...
                img_grid_data, img_received, IMAGE_GRID_ROWS, IMAGE_GRID_COLS, info_h, info_w, ...
                video_frame_data, video_received, VIDEO_FRAME_NUM, ...
                text_pkt_received, text_pkt_data, text_total_pkts);
            session_data_saved = true;
            rebuild_status_txt = sprintf('传输完成并已保存: 全部 %d/%d 块 | %s', total_recv, total_blocks, save_status);
            fprintf('[RX-STATE] COMPLETE: 全部 %d 块收齐并已保存\n', total_blocks);
        end
    end

    % ---------- 状态文本 ----------
    if state == STATE_COMPLETE
        if ~session_data_saved
            rebuild_status_txt = sprintf('传输完成: 全部 %d/%d 块', total_recv, total_blocks);
        end
        % 已保存时保留 save_session_data 设置的 rebuild_status_txt
    elseif has_text
        rebuild_status_txt = sprintf('文本收包: %d/%d', text_recv, text_total_pkts);
    else
        parts = {};
        if has_image, parts{end+1} = sprintf('图片%d/%d', img_recv, img_total); end
        if has_video, parts{end+1} = sprintf('视频%d/%d', vid_recv, vid_total); end
        rebuild_status_txt = ['收块中: ', strjoin(parts, ' | '), ...
            sprintf(' | 缺失%d | 重复%d', missing_num, dup_pkt_count)];
    end

    % ---------- UI ----------
    if rx_ui.enable && mod(idx, rx_ui.post_period) == 0
        % 有新块且距上次生成预览超过3帧时，生成部分预览图
        if total_recv > 0 && (idx - last_preview_gen) > 3
            preview_image_path = gen_preview_image(has_image, has_video, ...
                img_grid_data, img_received, IMAGE_GRID_ROWS, IMAGE_GRID_COLS, info_h, info_w, ...
                video_frame_data, video_received, VIDEO_FRAME_NUM, script_dir);
            last_preview_gen = idx;
        end
        % 保存后的预览图优先使用（避免传输完成后预览变黑）
        if isempty(preview_image_path) && ~isempty(last_save_preview_path)
            preview_image_path = last_save_preview_path;
        end
        rx_payload_ui = build_rx_ui_payload( ...
            Data_Rec_signal, Rec_sig_afr, CenterFrequency, samp_rate, FREQ_OFFSET, ...
            state, rebuild_status_txt, preview_image_path, ...
            Carrier_select_bef, Trans_power_select_bef, Power_gain_select_bef, Anti_Jamming_Mode_bef, RX_MODE, ...
            fb_success_rate, fb_tx_success, fb_tx_attempts);
        rx_ui = ui_try_post(rx_ui, '/api/data', rx_payload_ui);
    end

    if mod(idx, 10) == 0
        if has_text
            fprintf('[RX] idx=%d | state=%d | text=%d/%d | dup=%d\n', ...
                idx, state, text_recv, text_total_pkts, dup_pkt_count);
        elseif has_image && has_video
            fprintf('[RX] idx=%d | state=%d | img=%d/%d | vid=%d/%d | dup=%d\n', ...
                idx, state, img_recv, img_total, vid_recv, vid_total, dup_pkt_count);
        elseif has_image
            fprintf('[RX] idx=%d | state=%d | img=%d/%d | dup=%d\n', ...
                idx, state, img_recv, img_total, dup_pkt_count);
        else
            fprintf('[RX] idx=%d | state=%d | vid=%d/%d | dup=%d\n', ...
                idx, state, vid_recv, vid_total, dup_pkt_count);
        end
    end

    % 持续监听：不再因超时退出，等待新会话/新任务
    if state == STATE_COMPLETE && mod(idx, 200) == 0
        fprintf('[RX] 传输完成，持续监听中... (idx=%d)\n', idx);
    end
end

release(radio_rx);
release(radio_tx);

%% ================= 保存恢复结果 =================
save_dir = fullfile(script_dir, 'recovered');
if ~exist(save_dir, 'dir')
    mkdir(save_dir);
end

if has_image
    full_img = build_full_image(img_grid_data, img_received, IMAGE_GRID_ROWS, IMAGE_GRID_COLS, info_h, info_w);
    img_save_path = fullfile(save_dir, 'recovered_image.jpg');
    imwrite(full_img, img_save_path, 'JPEG');
    fprintf('[RX-SAVE] 图片已保存: %s (%d/%d 块)\n', img_save_path, sum(img_received(:)), img_total);
end

if has_video
    vid_save_path = fullfile(save_dir, 'recovered_video.avi');
    vw = VideoWriter(vid_save_path, 'Motion JPEG AVI');
    vw.FrameRate = 5;
    open(vw);
    for f = 1:VIDEO_FRAME_NUM
        if video_received(f) && ~isempty(video_frame_data{f})
            try
                frame_img = imdecode(video_frame_data{f});
                if ~isempty(frame_img)
                    writeVideo(vw, frame_img);
                else
                    writeVideo(vw, zeros(120, 160, 3, 'uint8'));
                end
            catch
                writeVideo(vw, zeros(120, 160, 3, 'uint8'));
            end
        else
            writeVideo(vw, zeros(120, 160, 3, 'uint8'));
        end
    end
    close(vw);
    fprintf('[RX-SAVE] 视频已保存: %s (%d/%d 帧)\n', vid_save_path, sum(video_received(:)), vid_total);
end

if has_text
    fprintf('\n===== 接收到的文本 =====\n');
    if text_total_pkts > 0
        all_bytes = [];
        for p = 1:text_total_pkts
            if text_pkt_received(p) && ~isempty(text_pkt_data{p})
                all_bytes = [all_bytes; text_pkt_data{p}(:)];
            end
        end
        % 去除尾部零填充
        last_nonzero = find(all_bytes > 0, 1, 'last');
        if ~isempty(last_nonzero)
            all_bytes = all_bytes(1:last_nonzero);
        end
        try
            txt = native2unicode(all_bytes(:), 'UTF-8');
            fprintf('%s\n', txt);
        catch
            fprintf('%s\n', char(all_bytes(:)'));
        end
    end
    fprintf('========================\n');
    fprintf('[RX-TEXT] 文本接收完成 (%d/%d 包)\n', text_recv, text_total_pkts);
end

fprintf('[RX-SAVE] 恢复文件输出至: %s\n', save_dir);

%% ================= 局部函数 =================
function preview_path = gen_preview_image(has_image, has_video, ...
    img_grid_data, img_received, grid_rows, grid_cols, img_h, img_w, ...
    video_frame_data, video_received, video_frame_count, script_dir)

preview_path = '';
save_dir = fullfile(script_dir, 'recovered');
if ~exist(save_dir, 'dir')
    mkdir(save_dir);
end

if has_image
    full_img = build_full_image(img_grid_data, img_received, grid_rows, grid_cols, img_h, img_w);
    preview_path = fullfile(save_dir, 'recovered_image_preview.jpg');
    imwrite(full_img, preview_path, 'JPEG');
end

if has_video && isempty(preview_path)
    for f = 1:video_frame_count
        if video_received(f) && ~isempty(video_frame_data{f})
            frame_img = imdecode(video_frame_data{f});
            if ~isempty(frame_img)
                preview_path = fullfile(save_dir, 'recovered_video_preview.jpg');
                imwrite(frame_img, preview_path, 'JPEG');
                break;
            end
        end
    end
end
end

function full_img = build_full_image(img_grid_data, img_received, grid_rows, grid_cols, img_h, img_w)
block_h = floor(img_h / grid_rows);
block_w = floor(img_w / grid_cols);
full_img = zeros(img_h, img_w, 3, 'uint8');
for r = 1:grid_rows
    for c = 1:grid_cols
        y1 = (r-1)*block_h + 1; y2 = r*block_h;
        x1 = (c-1)*block_w + 1; x2 = c*block_w;
        if img_received(r, c) && ~isempty(img_grid_data{r, c})
            try
                block_img = imdecode(img_grid_data{r, c});
                if ~isempty(block_img)
                    block_img = imresize(block_img, [block_h, block_w]);
                    full_img(y1:y2, x1:x2, :) = block_img;
                end
            catch
            end
        end
    end
end
end

function img = imdecode(jpeg_bytes)
% 将JPEG字节流解码为图像矩阵
tmp_name = [tempname, '.jpg'];
fid = fopen(tmp_name, 'wb');
if fid == -1
    img = [];
    return;
end
fwrite(fid, jpeg_bytes, 'uint8');
fclose(fid);
try
    img = imread(tmp_name);
catch
    img = [];
end
delete(tmp_name);
end

function safe_release_rx(tx, rx)
try
    if ~isempty(tx) && isvalid(tx)
        release(tx);
    end
catch
end
try
    if ~isempty(rx) && isvalid(rx)
        release(rx);
    end
catch
end
disp('接收端 SDR 资源已释放。');
end

function ui = ui_init(ui)
ui.post_future = [];
ui.has_bg = false;
try
    ui.has_bg = ((exist('backgroundPool', 'builtin') == 5) || (exist('backgroundPool', 'file') == 2)) && ...
                ((exist('parfeval', 'builtin') == 5) || (exist('parfeval', 'file') == 2));
catch
    ui.has_bg = false;
end

try
    opts = weboptions('Timeout', ui.timeout);
    webread([ui.url, ui.health_endpoint], opts);
    fprintf('[RX-UI] UI 已连接: %s\n', ui.url);
catch
    fprintf('[RX-UI] UI 未连接，不影响主程序: %s\n', ui.url);
end
end

function dec = ui_try_get_decision(ui, cur_carrier, cur_mode, cur_power)
% 改用POST方式同步获取决策（与旧版decision_making_flask一致）
persistent webwrite_opts call_count
if isempty(webwrite_opts)
    webwrite_opts = weboptions('RequestMethod', 'post', 'MediaType', 'application/json', 'Timeout', 3);
    call_count = 0;
end

dec = struct('anti_jamming_mode', 0, 'carrier_select', 3, 'power_gain_select', 1, 'needs_update', false);
if ~ui.enable
    return;
end

state_struct.Carrier_select_cur  = cur_carrier;
state_struct.Anti_Jamming_Mode   = cur_mode;
state_struct.Power_gain_cur      = cur_power;

call_count = call_count + 1;
try
    r = webwrite([ui.url, '/api/decision'], state_struct, webwrite_opts);
    if isstruct(r) && isfield(r, 'Par_valid') && r.Par_valid == 1
        if isfield(r, 'Carrier_select_desion')
            dec.carrier_select = r.Carrier_select_desion;
        end
        if isfield(r, 'Anti_Jamming_Mode_desion')
            dec.anti_jamming_mode = r.Anti_Jamming_Mode_desion;
        end
        if isfield(r, 'Power_gain_desion')
            dec.power_gain_select = r.Power_gain_desion;
        end
        dec.needs_update = true;
        fprintf('[RX-DEC] POST决策#%d: Par_valid=1 | carrier=%d | mode=%d | gain=%d\n', ...
            call_count, dec.carrier_select, dec.anti_jamming_mode, dec.power_gain_select);
    end
catch ME
    if mod(call_count, 10) == 1
        fprintf('[RX-DEC] POST失败#%d: %s\n', call_count, ME.message);
    end
end
end

function dec = ui_try_get_decision_async(ui, loop_idx, cur_carrier, cur_mode, cur_power)
% 非阻塞异步决策轮询：用 parfeval 后台 HTTP，不阻塞 SDR 循环
% 首次调用发起请求，后续调用检查结果 — 重叠 HTTP 延迟与 SDR 帧处理
persistent async_future async_call_count async_last_result async_armed
if isempty(async_future)
    async_future = [];
    async_call_count = 0;
    async_last_result = struct('anti_jamming_mode', 0, 'carrier_select', 3, ...
        'power_gain_select', 1, 'needs_update', false);
    async_armed = false;  % 是否有请求正在飞行中
end

dec = struct('anti_jamming_mode', 0, 'carrier_select', 3, 'power_gain_select', 1, 'needs_update', false);
if ~ui.enable
    return;
end

can_use_bg = ui.has_bg && ...
    ((exist('backgroundPool', 'builtin') == 5) || (exist('backgroundPool', 'file') == 2)) && ...
    ((exist('parfeval', 'builtin') == 5) || (exist('parfeval', 'file') == 2));

% 检查上一次异步请求是否完成
if ~isempty(async_future) && can_use_bg
    try
        st = async_future.State;
        if strcmp(st, 'finished')
            async_armed = false;
            try
                result = fetchOutputs(async_future);
                r = result{1};
                if isstruct(r) && isfield(r, 'Par_valid') && r.Par_valid == 1
                    if isfield(r, 'Carrier_select_desion')
                        dec.carrier_select = r.Carrier_select_desion;
                    end
                    if isfield(r, 'Anti_Jamming_Mode_desion')
                        dec.anti_jamming_mode = r.Anti_Jamming_Mode_desion;
                    end
                    if isfield(r, 'Power_gain_desion')
                        dec.power_gain_select = r.Power_gain_desion;
                    end
                    dec.needs_update = true;
                    async_call_count = async_call_count + 1;
                    fprintf('[RX-DEC-ASYNC] 决策#%d: carrier=%d | mode=%d | gain=%d (loop=%d)\n', ...
                        async_call_count, dec.carrier_select, dec.anti_jamming_mode, dec.power_gain_select, loop_idx);
                end
            catch
            end
            async_future = [];
        elseif strcmp(st, 'failed')
            async_armed = false;
            async_future = [];
        end
    catch
        async_future = [];
        async_armed = false;
    end
end

% 如果没有请求在飞行中，发射新的异步请求
if ~async_armed && can_use_bg
    try
        state_struct.Carrier_select_cur = cur_carrier;
        state_struct.Anti_Jamming_Mode  = cur_mode;
        state_struct.Power_gain_cur     = cur_power;
        async_future = parfeval(backgroundPool, @local_webwrite_decision, 1, ...
            [ui.url, '/api/decision'], state_struct);
        async_armed = true;
    catch
        % parfeval 不可用，回退到同步模式
        dec = ui_try_get_decision(ui, cur_carrier, cur_mode, cur_power);
    end
elseif ~can_use_bg
    % 无后台池，回退同步
    dec = ui_try_get_decision(ui, cur_carrier, cur_mode, cur_power);
end
end

function r = local_webwrite_decision(url, state_struct)
try
    opts = weboptions('RequestMethod', 'post', 'MediaType', 'application/json', 'Timeout', 3);
    r = webwrite(url, state_struct, opts);
catch
    r = struct('Par_valid', 0);
end
end

function ui = ui_try_post(ui, endpoint, payload)
if ~ui.enable
    return;
end

if ui.has_bg
    try
        can_submit = true;
        if ~isempty(ui.post_future)
            try
                st = ui.post_future.State;
                can_submit = strcmp(st, 'finished') || strcmp(st, 'failed');
            catch
                can_submit = true;
            end
        end
        if can_submit
            ui.post_future = parfeval(backgroundPool, @local_post_json, 0, [ui.url, endpoint], payload, ui.timeout);
        end
        % 若上一帧仍在发送中，静默丢弃本帧（丢帧优于阻塞 SDR 循环）
        return;
    catch
    end
end

try
    opts = weboptions('RequestMethod', 'post', 'MediaType', 'application/json', 'Timeout', ui.timeout);
    webwrite([ui.url, endpoint], payload, opts);
catch
end
end

function local_post_json(url, payload, timeout_val)
try
    opts = weboptions('RequestMethod', 'post', 'MediaType', 'application/json', 'Timeout', timeout_val);
    webwrite(url, payload, opts);
catch
end
end

function data = build_rx_ui_payload(rx_sig, rec_afr, CenterFrequency, samp_rate, FREQ_OFFSET, state, rebuild_status_txt, image_path, Carrier_select_bef, Trans_power_select_bef, Power_gain_select_bef, Anti_Jamming_Mode_bef, RX_MODE, fb_success_rate, fb_tx_success, fb_tx_attempts)
mode_names = {'仅图像', '仅视频', '图像+视频', '文本'};
N = 2048;

fft_start = max(1, floor(length(rx_sig) / 4));
if fft_start + N - 1 > length(rx_sig)
    fft_start = length(rx_sig) - N + 1;
end
fft_segment = rx_sig(fft_start : fft_start + N - 1);
% 加 Hann 窗抑制 LO 单音频谱泄露，避免旁瓣污染整个瀑布图
win = hann(N, 'periodic');
sig_fft = fftshift(fft(fft_segment .* win, N));
f_freq_kHz = (-N/2:N/2-1) * (samp_rate / N) / 1e3;
% LO 泄露已被频率偏移搬离信号，此处彻底剔除 LO 所在信道区域
lo_bin_offset = round(FREQ_OFFSET / samp_rate * N);  % 130kHz → ≈682 bin
dc_center = N/2 + 1;  % fftshift 后 DC = 1025
lo_bin_neg = dc_center - lo_bin_offset;  % LO 所在 bin
% 用远离 LO 区域的中值替换整个 LO 信道，确保信道热力图不受影响
bins_per_ch = round(N / 10);  % ≈205 bin/信道
lo_ch_start = lo_bin_neg - bins_per_ch/2;
lo_ch_end   = lo_bin_neg + bins_per_ch/2;
lo_ch_start = max(3, round(lo_ch_start));
lo_ch_end   = min(N-2, round(lo_ch_end));
% 取 LO 信道两侧各一个信道宽度的 bin 做参考中值
ref_bins = [max(3, lo_ch_start-bins_per_ch):lo_ch_start-1, ...
            lo_ch_end+1:min(N-2, lo_ch_end+bins_per_ch)];
ref_median = median(abs(sig_fft(ref_bins)));
% 将 LO 信道所有 bin 替换为参考中值（保持相位连续用原相位）
for k = lo_ch_start:lo_ch_end
    sig_fft(k) = ref_median * exp(1j * angle(sig_fft(k)));
end
% 使用绝对功率值（不归一化），避免最大值永远是0dB
sig_amp_dB = 20 * log10(abs(sig_fft) + 1e-10);

td_start = max(1, floor(length(rx_sig) / 5));
td_len = length(rx_sig) - td_start + 1;
step = max(1, floor(td_len / 300));  % 降采样到~300点减少JSON体积
t = (0:step:td_len-1) / samp_rate;

state_names = {'UNKNOWN', 'COLLECT', 'COMPLETE'};
if state >= 1 && state <= 2
    mode_text = state_names{state + 1};
else
    mode_text = sprintf('STATE_%d', state);
end

par_txt = sprintf('Carrier=%d | PowerIdx=%d | GainIdx=%d | Mode=%d', ...
    Carrier_select_bef, Trans_power_select_bef, Power_gain_select_bef, Anti_Jamming_Mode_bef);

data = struct();
data.spectrum.freq = reshape(f_freq_kHz, 1, []);
data.spectrum.amp  = reshape(sig_amp_dB, 1, []);
data.spectrum.linear = reshape(abs(sig_fft), 1, []);  % 线性幅度，用于信道占用计算
data.time_domain.time  = reshape(t, 1, []);
data.time_domain.amp   = reshape(abs(rx_sig(td_start:step:end)), 1, []);
if ~isscalar(rec_afr) && ~isempty(rec_afr)
    step_const = max(1, floor(length(rec_afr) / 1000));
    data.constellation.i = reshape(real(rec_afr(1:step_const:end)), 1, []);
    data.constellation.q = reshape(imag(rec_afr(1:step_const:end)), 1, []);
else
    data.constellation.i = [];
    data.constellation.q = [];
end
data.waterfall_line    = reshape(sig_amp_dB, 1, []);
data.waterfall_linear  = reshape(abs(sig_fft), 1, []);  % 线性幅度，用于时频图

data.status = struct();
data.status.data_rec_valid = '有效';
data.status.rx_mode_name = mode_names{RX_MODE};
data.status.current_send_mode = mode_text;
data.status.current_mod = 'QPSK/BPSK自适应';
data.status.center_frequency = CenterFrequency;
data.status.samp_rate = samp_rate;
data.status.snr = '--';
data.status.mes_valid = ['参数链路: ', par_txt];
data.status.mes_rate = 0;
data.status.power_gain = '--';
data.status.carrier_gain = sprintf('%.2f GHz', CenterFrequency / 1e9);
data.status.ber = '未测试';
data.status.current_time = datestr(now, 'HH:MM:SS');
data.status.received_text = rebuild_status_txt;
if fb_tx_attempts > 0
    data.status.fb_health = sprintf('%.1f%% (%d/%d)', fb_success_rate, fb_tx_success, fb_tx_attempts);
else
    data.status.fb_health = '空闲';
end

data.image_rebuild_status = rebuild_status_txt;
if ~isempty(image_path)
    data.received_image = image_path;
else
    data.received_image = '';  % 显式发送空值，通知Python端清除旧图片
end
end

function [status_txt, preview_path] = save_session_data(script_dir, has_image, has_video, has_text, ...
    img_grid_data, img_received, grid_rows, grid_cols, img_h, img_w, ...
    video_frame_data, video_received, video_frame_count, ...
    text_pkt_received, text_pkt_data, text_total_pkts)
% 保存当前会话的接收数据到 recovered 文件夹，返回状态文本和预览图路径

save_dir = fullfile(script_dir, 'recovered');
if ~exist(save_dir, 'dir')
    mkdir(save_dir);
end

status_parts = {};
preview_path = '';

if has_image
    full_img = build_full_image(img_grid_data, img_received, grid_rows, grid_cols, img_h, img_w);
    img_save_path = fullfile(save_dir, 'recovered_image.jpg');
    imwrite(full_img, img_save_path, 'JPEG');
    preview_path = img_save_path;
    n_recv = sum(img_received(:));
    n_total = grid_rows * grid_cols;
    fprintf('[RX-SAVE] 图片已保存: %s (%d/%d 块)\n', img_save_path, n_recv, n_total);
    status_parts{end+1} = sprintf('图片已保存(%d/%d)', n_recv, n_total);
end

if has_video && video_frame_count > 0
    vid_save_path = fullfile(save_dir, 'recovered_video.avi');
    n_recv = sum(video_received(:));
    try
        vw = VideoWriter(vid_save_path, 'Motion JPEG AVI');
        vw.FrameRate = 5;
        open(vw);
        for f = 1:video_frame_count
            if video_received(f) && ~isempty(video_frame_data{f})
                try
                    frame_img = imdecode(video_frame_data{f});
                    if ~isempty(frame_img)
                        writeVideo(vw, frame_img);
                    else
                        writeVideo(vw, zeros(120, 160, 3, 'uint8'));
                    end
                catch
                    writeVideo(vw, zeros(120, 160, 3, 'uint8'));
                end
            else
                writeVideo(vw, zeros(120, 160, 3, 'uint8'));
            end
        end
        close(vw);
        fprintf('[RX-SAVE] 视频已保存: %s (%d/%d 帧)\n', vid_save_path, n_recv, video_frame_count);
        status_parts{end+1} = sprintf('视频已保存(%d/%d)', n_recv, video_frame_count);
        % 视频模式下生成预览图，供Python UI显示
        if isempty(preview_path)
            for f = 1:video_frame_count
                if video_received(f) && ~isempty(video_frame_data{f})
                    frame_img = imdecode(video_frame_data{f});
                    if ~isempty(frame_img)
                        preview_path = fullfile(save_dir, 'recovered_video_preview.jpg');
                        imwrite(frame_img, preview_path, 'JPEG');
                        break;
                    end
                end
            end
        end
    catch ME
        fprintf('[RX-SAVE] 视频保存失败: %s\n', ME.message);
        status_parts{end+1} = '视频保存失败';
    end
end

if has_text && text_total_pkts > 0
    n_recv = sum(text_pkt_received(:));
    all_bytes = [];
    for p = 1:text_total_pkts
        if text_pkt_received(p) && ~isempty(text_pkt_data{p})
            all_bytes = [all_bytes; text_pkt_data{p}(:)];
        end
    end
    last_nonzero = find(all_bytes > 0, 1, 'last');
    if ~isempty(last_nonzero)
        all_bytes = all_bytes(1:last_nonzero);
    end
    txt_save_path = fullfile(save_dir, 'recovered_text.txt');
    try
        fid = fopen(txt_save_path, 'wb');
        if fid ~= -1
            fwrite(fid, all_bytes, 'uint8');
            fclose(fid);
            fprintf('[RX-SAVE] 文本已保存: %s (%d/%d 包)\n', txt_save_path, n_recv, text_total_pkts);
            status_parts{end+1} = sprintf('文本已保存(%d/%d)', n_recv, text_total_pkts);
        end
    catch
    end
    try
        txt = native2unicode(all_bytes(:), 'UTF-8');
        fprintf('===== 接收到的文本 =====\n%s\n========================\n', txt);
    catch
    end
end

if isempty(status_parts)
    status_txt = '无数据可保存';
else
    status_txt = strjoin(status_parts, ' | ');
end
end
