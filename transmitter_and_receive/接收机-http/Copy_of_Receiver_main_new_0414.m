% ===========全双工接收机 (合并版：AI Agent + 分块渐进式媒体传输)==============
clear
clc
close all force
warning('off', 'all');
% 清除持久化变量，确保 System objects 以正确的复数数据类型重新初始化
clear Data_Rece_sig_Gen Par_Rece_sig_Gen Par_trans_sig_Gen Data_trans_sig_Gen;
mod_selection = 1; % 0: 低速抗扰 1: 增益模式 2: 切频模式
Anti_Jamming_Mode_bef = 0; % 0: 常规模式 1: 低速抗扰模式 2: 切频模式
BER_test = 0;      % 误码率测试开关
recovered_image_path = ''; % 重建的图片路径

% ===== 媒体接收模式 =====
RX_MODE = 1;  % 1=仅图像, 2=仅视频, 3=图像+视频, 4=文本
IMAGE_GRID_ROWS = 8;
IMAGE_GRID_COLS = 8;
VIDEO_FRAME_NUM = 20;
TIMEOUT_IDLE = 50;  % 连续无新数据的空闲轮数，超时自动结束
CONTROL_TX_INTERVAL = 20;  % 反向链路发送间隔

%% =================初始化==================
if exist('radio_tx', 'var')
    if isvalid(radio_tx)
        release(radio_tx);
    end
    clear radio_tx;
end
if exist('radio_rx', 'var')
    if isvalid(radio_rx)
        release(radio_rx);
    end
    clear radio_rx;
end
if exist('qpskmod', 'var')
    if isvalid(qpskmod)
        release(qpskmod);
    end
    clear qpskmod;
end
if exist('qpskdemod', 'var')
    if isvalid(qpskdemod)
        release(qpskdemod);
    end
    clear qpskdemod;
end

%% ======================参数======================
Threshold = 300;
sps = 4;
samp_rate = 200e6/512;
ts=1/samp_rate;
SamplesPerFrame = 40000;

%% =========== SDR 硬件配置 (USRP X310) ===========
disp('正在初始化 USRP 硬件，请稍候...');
% 发射机配置
radio_tx = comm.SDRuTransmitter('Platform','X310','IPAddress','192.168.10.2');
radio_tx.ChannelMapping = 1;
radio_tx.CenterFrequency = 1.45*1e9;
radio_tx.Gain = 15;
radio_tx.MasterClockRate = 200e6;
radio_tx.InterpolationFactor = 512;
radio_tx.ClockSource = 'Internal';
radio_tx.UnderrunOutputPort = true;

% 接收机配置
radio_rx = comm.SDRuReceiver(...
    'Platform','X310', ...
    'IPAddress','192.168.10.2', ...
    'OutputDataType','double', ...
    'MasterClockRate',200e6, ...
    'DecimationFactor',512, ...
    'SamplesPerFrame',SamplesPerFrame);
radio_rx.ClockSource = 'Internal';
radio_rx.ChannelMapping = 1;
radio_rx.Gain = 10;
radio_rx.OverrunOutputPort = true;

% 注册清理函数，防止程序异常中断时占用 USRP 资源
cleanupObj = onCleanup(@() safe_release(radio_tx, radio_rx));
disp('USRP 硬件初始化完成！');

%% ===========业务参数部分===================
Carrier_set=[2e9:0.5e9:4e9];
Power_gain_set=[0:1:30];
Carrier_select_bef=3;
Trans_power_select_bef=3;
Power_gain_select_bef=1;

%% ============配置初始发送数据==============
Par_Trans_sig = Par_trans_sig_Gen(Anti_Jamming_Mode_bef,Carrier_select_bef,Trans_power_select_bef,Power_gain_select_bef);

%% ======插0（帧间隔）==========
trans_sigs_sample_num = 15000;
zero_pad_num_par = trans_sigs_sample_num-length(Par_Trans_sig)-2000;
Par_trans_temp = [zeros(zero_pad_num_par,1);Par_Trans_sig;zeros(2000,1)];
trans_sigs = Par_trans_temp;

Total_err_bit_num= 1;
Total_bit_num=0;
Par_valid = 0;
trans_flag = 0;
str_rec_rec = '等待接收...';
mes_valid = '无效';
Power_gain_txt = num2str(Power_gain_set(Power_gain_select_bef));
Carrier_gain_txt = num2str(Carrier_set(Carrier_select_bef)/1e9);
CenterFrequency = Carrier_set(Carrier_select_bef);

% 设定接收机初始中心频率
radio_rx.CenterFrequency = CenterFrequency;

BER_txt = '未测试';
refresh_num = 25;
SNR_dB_matrix = ones(1,refresh_num) * 30;
SNR_dB = 0;

%% ========== 媒体预存（分块渐进式传输） ==========
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

img_path = fullfile(script_dir, 'p2.jpg');
video_path = fullfile(script_dir, '视频.mp4');

if has_image
    if ~exist(img_path, 'file')
        warning('[RX-INIT] 图片文件不存在: %s，将跳过图片接收。请将 p2.jpg 放置到脚本目录。', img_path);
        has_image = false;
    else
        fprintf('[RX-INIT] 预存图片块数据...\n');
        [img_blocks, img_crc] = preprocess_image(img_path, IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
        [info_h, info_w] = get_image_dims(img_path);
        img_received = false(IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
        img_grid_data = cell(IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
    end
else
    img_blocks = {}; img_crc = []; info_h = 240; info_w = 320;
    img_received = []; img_grid_data = {};
end

if has_video
    if ~exist(video_path, 'file')
        warning('[RX-INIT] 视频文件不存在: %s，将跳过视频接收。请将 视频.mp4 放置到脚本目录。', video_path);
        has_video = false;
    else
        fprintf('[RX-INIT] 预存视频帧数据...\n');
        [video_blocks_data, video_crc] = preprocess_video(video_path, VIDEO_FRAME_NUM);
        video_received = false(VIDEO_FRAME_NUM, 1);
        video_frame_data = cell(VIDEO_FRAME_NUM, 1);
    end
else
    video_blocks_data = {}; video_crc = [];
    video_received = []; video_frame_data = {};
end

if has_text
    text_total_pkts = 0;
    text_pkt_received = [];
    text_pkt_data = {};
end

img_total = IMAGE_GRID_ROWS * IMAGE_GRID_COLS;
vid_total = VIDEO_FRAME_NUM;
fprintf('[RX-INIT] 预存完成 | 图片 %d块 | 视频 %d帧\n', img_total, vid_total);

%% ========== 接收缓存与状态 ==========
rx_session_id = 0;
rx_total_pkt_num = 0;
rx_pkt_valid = false(1, 10000);
img_rebuild_done = false;
rebuild_status_txt = '等待接收图片分块...';
STATE_COLLECT  = 1;
STATE_COMPLETE = 2;
state = STATE_COLLECT;

prev_img_recv = 0;
prev_vid_recv = 0;
prev_text_recv = 0;
dup_pkt_count = 0;
last_progress_idx = 0;
total_recv = 0;

%% ========== 瀑布图历史矩阵初始化 ==========
history_len = 80;
N_fft = 2048;

%% ========== 状态机追踪与冷却变量 ==========
cooldown_counter = 0;
last_decision_version = 0;

%% =========== 与 Python Flask 建立连接 ===========
PYTHON_URL = 'http://127.0.0.1:5000';
disp(['正在连接 Python Flask 服务器: ', PYTHON_URL, ' ...']);

webread_opts_main = weboptions('Timeout', 5);
try
    response = webread([PYTHON_URL, '/api/health'], webread_opts_main);
    disp('连接 Python Flask 成功！');
catch
    warning('无法连接到 Python Flask 服务器，请确保 Python 脚本已运行。UI将无法更新。');
end

%% ================= 主循环 =================
for idx=1:100000
    %% --- 从Python获取控制指令 (UI或AI Agent) ---
    if mod(idx, 10) == 0
        try
            decision_response = webread([PYTHON_URL, '/api/decision'], webread_opts_main);

            has_update = false;
            if isfield(decision_response, 'needs_update')
                has_update = decision_response.needs_update;
            end
            if isfield(decision_response, 'decision_version')
                new_ver = decision_response.decision_version;
                if new_ver ~= last_decision_version
                    has_update = true;
                    fprintf('[决策轮询] 版本变化: %d -> %d\n', last_decision_version, new_ver);
                    last_decision_version = new_ver;
                end
            end

            if has_update
                changed = false;

                if isfield(decision_response, 'anti_jamming_mode')
                    new_mode = decision_response.anti_jamming_mode;
                    if new_mode ~= Anti_Jamming_Mode_bef
                        Anti_Jamming_Mode_bef = new_mode;
                        mode_names_list = {'常规模式', '低速抗扰模式', '切频模式'};
                        disp(['指令已切换模式: ', mode_names_list{new_mode + 1}]);
                        changed = true;
                    end
                end

                if isfield(decision_response, 'carrier_select')
                    new_carrier = decision_response.carrier_select;
                    if new_carrier >= 1 && new_carrier <= length(Carrier_set) && new_carrier ~= Carrier_select_bef
                        Carrier_select_bef = new_carrier;
                        CenterFrequency = Carrier_set(Carrier_select_bef);
                        radio_rx.CenterFrequency = CenterFrequency;
                        Carrier_gain_txt = num2str(Carrier_set(Carrier_select_bef)/1e9);
                        disp(['指令已切换频率: ', Carrier_gain_txt, ' GHz']);
                        changed = true;
                    end
                end

                if isfield(decision_response, 'power_gain_select')
                    new_power = decision_response.power_gain_select;
                    if new_power >= 1 && new_power <= length(Power_gain_set) && new_power ~= Power_gain_select_bef
                        Power_gain_select_bef = new_power;
                        Power_gain_txt = num2str(Power_gain_set(Power_gain_select_bef));
                        disp(['指令已调整功率: ', Power_gain_txt, ' dB']);
                        changed = true;
                    end
                end

                if isfield(decision_response, 'threshold')
                    new_thr = decision_response.threshold;
                    if new_thr ~= Threshold
                        Threshold = new_thr;
                        disp(['指令已更新阈值: ', num2str(Threshold)]);
                        changed = true;
                    end
                end

                if isfield(decision_response, 'mod_selection')
                    new_mod = decision_response.mod_selection;
                    if new_mod >= 0 && new_mod <= 2 && new_mod ~= mod_selection
                        mod_selection = new_mod;
                        disp(['指令已切换调制选择: ', num2str(mod_selection)]);
                        changed = true;
                    end
                end

                if changed
                    trans_flag = 1;
                    cooldown_counter = 50;
                    if Anti_Jamming_Mode_bef == 1
                        SNR_dB_matrix(:) = 0;
                    else
                        SNR_dB_matrix(:) = 30;
                    end
                end

                if isfield(decision_response, 'command_source') && isfield(decision_response, 'command_description')
                    src = decision_response.command_source;
                    desc = decision_response.command_description;
                    disp(['[', src, '] ', desc]);
                elseif ~changed
                    fprintf('[决策轮询] has_update=1 但参数未变化 (mode=%d, carrier=%d, power=%d)\n', ...
                        Anti_Jamming_Mode_bef, Carrier_select_bef, Power_gain_select_bef);
                end
            end
        catch ME
            if ~contains(ME.message, 'timeout') && ~contains(ME.message, 'Timeout')
                warning('控制指令获取失败: %s', ME.message);
            end
        end
    end

    %% --- 反向链路：周期性发送载频+SNR给发射端 ---
    if state == STATE_COMPLETE
        % 已完成，不发反向链路
    else
        if mod(idx, CONTROL_TX_INTERVAL) == 0 && exist('Data_Rec_signal', 'var')
            snr_val = snr_est(Data_Rec_signal);
            snr_db = 10 * log10(max(snr_val, 0.001));
            snr_byte = uint8(max(0, min(255, round(snr_db + 20))));
            fb_session = bitshift(uint16(Carrier_select_bef), 8) + uint16(snr_byte);

            Par_Trans_sig_fb = Par_trans_sig_Gen('feedback', ...
                20, fb_session, 0, 0, Anti_Jamming_Mode_bef);

            fb_sample_num = 60000;
            zero_pad_fb = fb_sample_num - length(Par_Trans_sig_fb) - 2000;
            zero_pad_fb = max(zero_pad_fb, 0);
            trans_sigs_fb = [zeros(zero_pad_fb,1); Par_Trans_sig_fb; zeros(2000,1)];

            if mod(idx, CONTROL_TX_INTERVAL * 5) == 0
                fprintf('[RX-REV] 反向链路: Carrier=%d | SNR~%.1f dB\n', Carrier_select_bef, snr_db);
            end
        end
    end

    %% --- 硬件数据收发 ---
    try
        [Data_Rec_signal, rx_len, rx_overrun] = radio_rx();
        % 反向链路帧优先，否则发送参数信道
        if exist('trans_sigs_fb', 'var') && mod(idx, CONTROL_TX_INTERVAL) == 0 && state ~= STATE_COMPLETE
            tx_underrun = radio_tx(trans_sigs_fb);
        else
            tx_underrun = radio_tx(trans_sigs);
        end

        if rx_overrun
            warning('接收机溢出 (Overrun) - MATLAB处理速度跟不上硬件采样率！');
        end
        if tx_underrun
            warning('发射机欠载 (Underrun) - MATLAB无法及时提供发射数据！');
        end
    catch ME
        warning(['硬件底层通信异常 (第 ', num2str(idx), ' 帧) - 已自动恢复... 错误信息: ', ME.message]);
        continue;
    end

    %% --- 信号处理与解调 ---
    [str_rec,Rec_sig_afr,data_flag,err_valid,err_bit_num,total_num,frame_packets] = Data_Rece_sig_Gen(Anti_Jamming_Mode_bef,Data_Rec_signal,BER_test,Threshold);

    if data_flag == 1
        data_rec_valid = '有效';
        if Rec_sig_afr~=1
            SNR = snr_est(Rec_sig_afr);
            SNR_dB_temp=10*log10(SNR);
            SNR_dB_matrix(1:end-1) = SNR_dB_matrix(2:end);
            SNR_dB_matrix(end) = SNR_dB_temp;
            SNR_dB = mean(SNR_dB_matrix);
        end
        SNR_TXT = num2str(SNR_dB);
        SNR_valid = 1;
    else
        data_rec_valid = '无效';
        SNR_dB = 0;
        SNR_TXT = '未捕获信号';
        SNR_valid = 0;
    end

    Carrier_max_num = length(Carrier_set);
    Power_gain_max_num = length(Power_gain_set);

    %% --- 数据解析与分块渐进式媒体重组 ---
    if data_flag == 1
        if ~isempty(frame_packets)
            pkt_num_this_round = length(frame_packets);

            for pkt_idx = 1:pkt_num_this_round
                pkt = frame_packets(pkt_idx);

                % --- 会话管理：新会话 → 重置所有状态 ---
                if rx_session_id == 0 || pkt.Session_ID ~= rx_session_id
                    rx_session_id = pkt.Session_ID;
                    rx_total_pkt_num = pkt.Total_frame_num;
                    rx_pkt_valid = false(1, max(10000, rx_total_pkt_num));

                    % --- 根据空口信令中的tx_mode动态切换接收模式 ---
                    if isfield(pkt, 'tx_mode') && pkt.tx_mode >= 1 && pkt.tx_mode <= 4
                        new_rx_mode = pkt.tx_mode;
                        if new_rx_mode ~= RX_MODE
                            fprintf('[RX-MODE] 信令链路触发模式切换: %d -> %d (%s -> %s)\n', ...
                                RX_MODE, new_rx_mode, mode_names{RX_MODE}, mode_names{new_rx_mode});
                            RX_MODE = new_rx_mode;
                            has_image = ismember(RX_MODE, [1, 3]);
                            has_video = ismember(RX_MODE, [2, 3]);
                            has_text  = (RX_MODE == 4);
                            % 通知Flask UI任务模式已变更
                            try
                                sync_data = struct('rx_mode', RX_MODE, ...
                                                   'tx_mode_name', mode_names{RX_MODE});
                                sync_opts = weboptions('RequestMethod', 'post', ...
                                    'MediaType', 'application/json', 'Timeout', 3);
                                webwrite([PYTHON_URL, '/api/task_sync'], jsonencode(sync_data), sync_opts);
                            catch
                            end
                        end
                    end

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
                    prev_img_recv = 0; prev_vid_recv = 0; prev_text_recv = 0;
                    dup_pkt_count = 0;
                    fprintf('[RX-SESSION] 新会话：session=%d | total=%d | mode=%s\n', ...
                        rx_session_id, rx_total_pkt_num, mode_names{RX_MODE});
                end

                % --- 去重 & 块元数据解析 + CRC校验 ---
                if pkt.Frame_num >= 1 && pkt.Frame_num <= length(rx_pkt_valid)
                    if ~rx_pkt_valid(pkt.Frame_num)

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
                                pkt_idx_txt = blk_row + 1;
                                total_pkts = pkt.block_total_rows;
                                if text_total_pkts == 0
                                    text_total_pkts = total_pkts;
                                    text_pkt_received = false(1, total_pkts);
                                    text_pkt_data = cell(1, total_pkts);
                                    fprintf('[RX-TEXT] 检测到文本传输: %d 个包\n', total_pkts);
                                end
                                if pkt_idx_txt >= 1 && pkt_idx_txt <= length(text_pkt_received) && ~text_pkt_received(pkt_idx_txt)
                                    rx_pkt_valid(pkt.Frame_num) = true;
                                    text_pkt_received(pkt_idx_txt) = true;
                                    text_pkt_data{pkt_idx_txt} = pkt.Payload_bytes(10:end);
                                end
                            end
                        else
                            % 无块元数据的普通帧：兼容旧格式
                        end
                    else
                        dup_pkt_count = dup_pkt_count + 1;
                    end
                end
            end

            % --- 进度统计 ---
            img_recv = 0; vid_recv = 0; text_recv = 0;
            if has_image, img_recv = sum(img_received(:)); end
            if has_video, vid_recv = sum(video_received(:)); end
            if has_text, text_recv = sum(text_pkt_received(:)); end
            total_recv = img_recv + vid_recv + text_recv;

            total_blocks = 0;
            if has_image, total_blocks = total_blocks + img_total; end
            if has_video, total_blocks = total_blocks + vid_total; end
            if has_text && text_total_pkts > 0, total_blocks = text_total_pkts; end
            missing_num = total_blocks - total_recv;

            progress_happened = (img_recv > prev_img_recv) || (vid_recv > prev_vid_recv) || (text_recv > prev_text_recv);
            prev_img_recv = img_recv;
            prev_vid_recv = vid_recv;
            if has_text, prev_text_recv = text_recv; end

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

            % --- 自适应阈值/增益（仅在无AI/UI指令冷却期生效） ---
            if cooldown_counter == 0
                if missing_num <= 8
                    Threshold = 195;
                    radio_rx.Gain = 18;
                elseif missing_num <= 32
                    Threshold = 210;
                    radio_rx.Gain = 16;
                else
                    Threshold = 240;
                    radio_rx.Gain = 10;
                end
            end

            % --- 状态文本 ---
            if state == STATE_COMPLETE
                rebuild_status_txt = sprintf('传输完成: 全部 %d/%d 块', total_recv, total_blocks);
            elseif has_text
                rebuild_status_txt = sprintf('文本收包: %d/%d', text_recv, text_total_pkts);
            else
                parts = {};
                if has_image, parts{end+1} = sprintf('图片%d/%d', img_recv, img_total); end
                if has_video, parts{end+1} = sprintf('视频%d/%d', vid_recv, vid_total); end
                rebuild_status_txt = ['收块中: ', strjoin(parts, ' | '), ...
                    sprintf(' | 缺失%d | 重复%d', missing_num, dup_pkt_count)];
            end
            str_rec_rec = rebuild_status_txt;

            % --- 完成判定 ---
            if state == STATE_COLLECT && missing_num == 0 && total_blocks > 0
                state = STATE_COMPLETE;
                img_rebuild_done = true;
                fprintf('[RX-STATE] COMPLETE: 全部 %d 块收齐\n', total_blocks);

                % 保存恢复结果
                save_dir = fullfile(script_dir, 'recovered');
                if ~exist(save_dir, 'dir')
                    mkdir(save_dir);
                end

                if has_image
                    full_img = build_full_image(img_grid_data, img_received, IMAGE_GRID_ROWS, IMAGE_GRID_COLS, info_h, info_w);
                    img_save_path = fullfile(save_dir, 'recovered_image.jpg');
                    imwrite(full_img, img_save_path, 'JPEG');
                    recovered_image_path = img_save_path;
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
                                frame_img = imdecode_jpeg(video_frame_data{f});
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
                end

                fprintf('[RX-SAVE] 恢复文件输出至: %s\n', save_dir);
            end
        else
            str_rec_rec = '检测到同步头，但当前 buffer 未解析出有效数据帧';
        end

        %% --- 智能抗干扰决策 (仅在收到有效信号时进行) ---
        if Anti_Jamming_Mode_bef == 1
            SNR_THR = [25, 16, 16];
        else
            SNR_THR = [16, 16, 16];
        end

        if mod(idx,refresh_num) == 0 && cooldown_counter == 0
            [Carrier_select_desion,Anti_Jamming_Mode_desion,Power_gain_desion,Par_valid] = decision_making_flask(PYTHON_URL, Anti_Jamming_Mode_bef, mod_selection, SNR_dB, SNR_THR, SNR_valid, Carrier_select_bef, Power_gain_select_bef, Carrier_max_num, Power_gain_max_num, BER_test);

            if Par_valid == 1
                mes_valid = '有效';
                changed = false;

                if Anti_Jamming_Mode_bef ~= Anti_Jamming_Mode_desion
                    Anti_Jamming_Mode_bef = Anti_Jamming_Mode_desion;
                    changed = true;
                end
                if Power_gain_select_bef ~= Power_gain_desion
                    Power_gain_select_bef = max(1, min(length(Power_gain_set), Power_gain_desion));
                    Power_gain_txt = num2str(Power_gain_set(Power_gain_select_bef));
                    changed = true;
                end
                if Carrier_select_bef ~= Carrier_select_desion
                    Carrier_select_bef = max(1, min(length(Carrier_set), Carrier_select_desion));
                    CenterFrequency = Carrier_set(Carrier_select_bef);
                    radio_rx.CenterFrequency = CenterFrequency;
                    Carrier_gain_txt = num2str(Carrier_set(Carrier_select_bef)/1e9);
                    changed = true;
                end

                if changed
                    trans_flag = 1;
                    cooldown_counter = 50;
                    if Anti_Jamming_Mode_bef == 1
                        SNR_dB_matrix(:) = 0;
                    else
                        SNR_dB_matrix(:) = 30;
                    end
                end
            else
                mes_valid = '无效';
            end
        end

        if BER_test == 1
            Total_err_bit_num = Total_err_bit_num + err_valid*err_bit_num;
            Total_bit_num = Total_bit_num + err_valid*total_num;
            if Total_bit_num>1e4
                BER = Total_err_bit_num/Total_bit_num;
                BER_txt = num2str(BER);
            else
                BER_txt = num2str(0);
            end
        else
            BER_txt = '未测试';
        end

        if trans_flag == 1
            Total_err_bit_num= 1;
            Total_bit_num=0;
            Par_Trans_sig = Par_trans_sig_Gen(Anti_Jamming_Mode_bef,Carrier_select_bef,Trans_power_select_bef,Power_gain_select_bef);
            trans_flag = 0;
            trans_sigs_sample_num = 15000;
            zero_pad_num_par = trans_sigs_sample_num-length(Par_Trans_sig)-2000;
            Par_trans_temp = [zeros(zero_pad_num_par,1);Par_Trans_sig;zeros(2000,1)];
            trans_sigs = Par_trans_temp;
        end
    end % 结束 data_flag == 1 逻辑

    if cooldown_counter > 0
        cooldown_counter = cooldown_counter - 1;
    end

    %% --- 空闲超时：连续无新数据则自动结束 ---
    if last_progress_idx > 0 && total_recv > 0 && (idx - last_progress_idx) > TIMEOUT_IDLE
        fprintf('[RX-TIMEOUT] 连续 %d 轮无新数据，自动结束恢复\n', TIMEOUT_IDLE);
        break;
    end

    %% --- 向 UI 发送数据 ---
    if data_flag == 1 || mod(idx, 2) == 0
        if data_flag == 1
            if Anti_Jamming_Mode_bef == 0
                current_mod = 'QPSK';
                current_send_mode ='常规模式';
            elseif  Anti_Jamming_Mode_bef == 1
                current_mod = 'BPSK';
                current_send_mode ='低速抗扰模式';
            elseif Anti_Jamming_Mode_bef == 2
                current_mod = 'QPSK';
                current_send_mode= '切频模式';
            else
                current_mod = 'QPSK';
                current_send_mode= '未知模式';
            end
        else
            current_mod = '未知';
            current_send_mode ='等待捕获同步信号...';
        end
        mes_rate = 96/(1/samp_rate*length(Par_Trans_sig));
        current_time = datestr(now, 'HH:MM:SS');
        fs = samp_rate;
        N = 2048;

        % 计算频谱与瀑布图（去DC + 自适应陷波抑制LO泄漏）
        Data_Rec_fft_input = Data_Rec_signal(1:N);
        Data_Rec_fft_input = Data_Rec_fft_input - mean(Data_Rec_fft_input);
        sig_fft = fftshift(fft(Data_Rec_fft_input, N));
        center_bin = N/2 + 1;
        notch_hw = 20;
        notch_idx = center_bin - notch_hw : center_bin + notch_hw;
        bg_bins = [center_bin-notch_hw-30:center_bin-notch_hw-1, ...
                   center_bin+notch_hw+1:center_bin+notch_hw+30];
        bg_level = median(abs(sig_fft(bg_bins)));
        peak_level = max(abs(sig_fft(notch_idx)));
        min_atten = max(bg_level / max(peak_level, 1e-10), 0.05);
        dist = abs(notch_idx - center_bin);
        cos_taper = min_atten + (1 - min_atten) * sin(pi/2 * dist / notch_hw).^2;
        sig_fft(notch_idx) = sig_fft(notch_idx) .* cos_taper(:);
        f_freq_kHz = (-N/2 : N/2-1) * (fs/N) / 1e3;
        sig_amp_dB = 20*log10(abs(sig_fft)/max(abs(sig_fft)) + 1e-10);

        sig_fft_1 = fftshift(fft(Par_Trans_sig, N));
        f_freq_kHz_mes = (-N/2 : N/2-1) * (fs/N) / 1e3;
        sig_amp_dB_mes = 20*log10(abs(sig_fft_1)/max(abs(sig_fft_1)) + 1e-10);

        sig_dB_waterfall = sig_amp_dB;
        time_idex_plot = 0:ts:(length(Data_Rec_signal)-1)*ts;

        step_td = max(1, floor(length(time_idex_plot) / 2000));

        frame_packets_struct = struct();
        if data_flag == 1 && ~isempty(frame_packets)
            for fp_idx = 1:length(frame_packets)
                frame_packets_struct(fp_idx).Frame_num = frame_packets(fp_idx).Frame_num;
                frame_packets_struct(fp_idx).Total_frame_num = frame_packets(fp_idx).Total_frame_num;
                frame_packets_struct(fp_idx).Payload_bytes = double(frame_packets(fp_idx).Payload_bytes);
                frame_packets_struct(fp_idx).ZeroPadding_num = frame_packets(fp_idx).ZeroPadding_num;
                frame_packets_struct(fp_idx).Session_ID = frame_packets(fp_idx).Session_ID;
                frame_packets_struct(fp_idx).block_row = frame_packets(fp_idx).block_row;
                frame_packets_struct(fp_idx).block_col = frame_packets(fp_idx).block_col;
                frame_packets_struct(fp_idx).block_type = frame_packets(fp_idx).block_type;
            end
        end

        data_to_send = struct();
        data_to_send.spectrum.freq = f_freq_kHz;
        data_to_send.spectrum.amp = sig_amp_dB;
        data_to_send.spectrum_mes.freq = f_freq_kHz_mes;
        data_to_send.spectrum_mes.amp = sig_amp_dB_mes;
        data_to_send.time_domain.time = time_idex_plot(1:step_td:end);
        data_to_send.time_domain.amp = abs(Data_Rec_signal(1:step_td:end));

        if data_flag == 1
            step_const = max(1, floor(length(Rec_sig_afr) / 1000));
            data_to_send.constellation.i = real(Rec_sig_afr(1:step_const:end));
            data_to_send.constellation.q = imag(Rec_sig_afr(1:step_const:end));
        else
            data_to_send.constellation.i = [];
            data_to_send.constellation.q = [];
        end
        data_to_send.waterfall_line = sig_dB_waterfall;
        data_to_send.status.data_rec_valid = data_rec_valid;
        data_to_send.status.current_send_mode = current_send_mode;
        data_to_send.status.rx_mode = RX_MODE;                % 当前接收任务模式(1-4)
        data_to_send.status.rx_mode_name = mode_names{RX_MODE};  % 任务模式中文名
        data_to_send.status.current_mod = current_mod;
        data_to_send.status.center_frequency = CenterFrequency;
        data_to_send.status.samp_rate = samp_rate;
        data_to_send.status.snr = SNR_TXT;
        data_to_send.status.mes_valid = mes_valid;
        data_to_send.status.mes_rate = mes_rate;
        data_to_send.status.power_gain = Power_gain_txt;
        data_to_send.status.carrier_gain = Carrier_gain_txt;
        data_to_send.status.ber = BER_txt;
        data_to_send.status.current_time = current_time;
        data_to_send.status.received_text = str_rec_rec;
        data_to_send.image_rebuild_status = rebuild_status_txt;
        if ~isempty(recovered_image_path)
            data_to_send.received_image = recovered_image_path;
        end
        if ~isempty(fieldnames(frame_packets_struct))
            data_to_send.frame_packets = frame_packets_struct;
        end

        send_data_to_python(PYTHON_URL, data_to_send);

        if data_flag == 1
            fprintf('解调成功: %s\n', char(str_rec));
        end
    end

    if mod(idx, 10) == 0
        fprintf('正在运行... 当前帧数: %d | 当前时间: %s\n', idx, datestr(now, 'HH:MM:SS'));
    end
end

% 正常结束时释放硬件
release(radio_rx);
release(radio_tx);
disp('主循环结束，硬件已释放。');

%% ================== 辅助函数 ==================
function safe_release(tx, rx)
    if exist('tx', 'var') && isvalid(tx)
        release(tx);
    end
    if exist('rx', 'var') && isvalid(rx)
        release(rx);
    end
    disp('SDR 硬件已安全释放。');
end

function send_data_to_python(url, data)
    persistent first_send webwrite_opts
    if isempty(first_send)
        first_send = true;
        webwrite_opts = weboptions('RequestMethod', 'post', 'MediaType', 'application/json', 'Timeout', 5);
    end

    try
        json_data = jsonencode(data);
        webwrite([url, '/api/data'], json_data, webwrite_opts);

        if first_send
            disp('数据已成功发送到Python UI！');
            first_send = false;
        end
    catch ME
        if first_send
            warning(['发送数据到Python UI失败，请检查Flask是否运行: ', ME.message]);
            first_send = false;
        elseif ~contains(ME.message, 'timeout') && ~contains(ME.message, 'Timeout')
            warning('数据发送到Python超时或失败: %s', ME.message);
        end
    end
end

function [Carrier_select_desion,Anti_Jamming_Mode_desion,Power_gain_desion,Par_valid] = decision_making_flask(url, Anti_Jamming_Mode, mod_selection, SNR, SNR_THR, SNR_valid, Carrier_select_cur, Power_gain_cur, Carrier_max_num, Power_gain_max_num, BER_test)
    persistent webwrite_opts_decision
    if isempty(webwrite_opts_decision)
        webwrite_opts_decision = weboptions('RequestMethod', 'post', 'MediaType', 'application/json', 'Timeout', 5);
    end

    Carrier_select_desion = Carrier_select_cur;
    Power_gain_desion = Power_gain_cur;
    Anti_Jamming_Mode_desion = Anti_Jamming_Mode;
    Par_valid = 0;

    if BER_test == 1 || SNR_valid == 0
        return;
    end

    state_struct.SNR = SNR;
    state_struct.mod_selection = mod_selection;
    state_struct.Anti_Jamming_Mode = Anti_Jamming_Mode;
    state_struct.Carrier_select_cur = Carrier_select_cur;
    state_struct.Power_gain_cur = Power_gain_cur;
    state_struct.Carrier_max_num = Carrier_max_num;
    state_struct.Power_gain_max_num = Power_gain_max_num;

    try
        json_data = jsonencode(state_struct);
        response = webwrite([url, '/api/decision'], json_data, webwrite_opts_decision);
        action_struct = jsondecode(response);

        Carrier_select_desion = action_struct.Carrier_select_desion;
        Anti_Jamming_Mode_desion = action_struct.Anti_Jamming_Mode_desion;
        Power_gain_desion = action_struct.Power_gain_desion;
        Par_valid = action_struct.Par_valid;
    catch ME
        if ~contains(ME.message, 'timeout') && ~contains(ME.message, 'Timeout')
            warning('决策请求失败: %s', ME.message);
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
                    block_img = imdecode_jpeg(img_grid_data{r, c});
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

function img = imdecode_jpeg(jpeg_bytes)
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
