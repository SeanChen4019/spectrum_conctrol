% =========== 发射端：分块渐进式传输 + 空闲等待 + 固定轮次 ===========
clear
clc
close all force
warning('off', 'all');

fprintf('\n==================== 发射端（分块渐进式传输）启动 ====================\n');

% 清除持久变量，避免 System object 复/实性冲突
clear Data_trans_sig_Gen Par_Rece_sig_Gen;

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
BUS_SLOT_SAMPLES = 160000;
FB_RX_SAMPLES    = 60000;

% 频率偏移 (Low-IF): 将基带信号搬离 DC，解决零中频 LO 泄露
% 偏移量需满足: offset + 信号带宽/2 < samp_rate/2
% 信号带宽 ≈ 122 kHz, Nyquist ≈ 195 kHz, 偏移 80 kHz 留有裕量
FREQ_OFFSET = 130e3;  % 130 kHz 基带偏移（LO 推到奈奎斯特边缘）

GUARD_PRE     = 8000;
GUARD_BETWEEN = 1200;
GUARD_POST    = 8000;

% ---- 新增：固定轮次 + 空闲等待 ----
NUM_ROUNDS       = 5;   % 多轮发送，给接收机足够时间锁定
IDLE_SLOTS       = 3;
IMAGE_GRID_ROWS  = 8;
IMAGE_GRID_COLS  = 8;
VIDEO_FRAME_NUM  = 20;

TX_MODE = 1;  % 1=仅图像, 2=仅视频, 3=图像+视频, 4=文本

TEXT_STRING = 'Hello World! 这是一段通过USRP无线传输的测试文本。';  % TX_MODE=4时发送的文本

STATE_SENDING = 1;
STATE_IDLE    = 2;
STATE_DONE    = 3;

Carrier_set = 2e9 : 0.5e9 : 4e9;
Power_set = 2e-1 : 1e-1 : 8e-1;
Power_gain_set = 0 : 1 : 30;

Anti_Jamming_Mode = 0;
% BURST_PKTS 按模式计算: QPSK 每包~3504采样, BPSK+扩频每包~41088采样

% 槽可用=144000, QPSK可装30包, BPSK仅3包
if Anti_Jamming_Mode == 1
    BURST_PKTS = 3;
else
    BURST_PKTS = 10;
end
Carrier_select_rec = 3;
Trans_power_select_rec = 7;

CenterFrequency = Carrier_set(Carrier_select_rec);
Power = Power_set(Trans_power_select_rec);
Power_gain_select_rec_bef = 15;
Power_gain = Power_gain_set(Power_gain_select_rec_bef);

Carrier_select_rec_bef = Carrier_select_rec;
Anti_Jamming_Mode_bef = Anti_Jamming_Mode;

script_dir = fileparts(mfilename('fullpath'));
if isempty(script_dir)
    script_dir = pwd;
end
addpath(script_dir);

%% ================= 预处理媒体为块（按 TX_MODE） =================
mode_names = {'仅图像', '仅视频', '图像+视频', '文本'};
fprintf('[TX-INIT] 发送模式: %s\n', mode_names{TX_MODE});

block_meta = [];
img_path = fullfile(script_dir, 'p2.jpg');
video_path = fullfile(script_dir, '视频.mp4');

if ismember(TX_MODE, [1, 3])
    fprintf('[TX-INIT] 预处理图片...\n');
    [~, img_crc] = preprocess_image(img_path, IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
    for r = 1:IMAGE_GRID_ROWS
        for c = 1:IMAGE_GRID_COLS
            blk.row = r - 1;
            blk.col = c - 1;
            blk.total_rows = IMAGE_GRID_ROWS;
            blk.total_cols = IMAGE_GRID_COLS;
            blk.crc32 = img_crc(r, c);
            blk.type = 0;
            block_meta = [block_meta, blk];
        end
    end
end

if ismember(TX_MODE, [2, 3])
    fprintf('[TX-INIT] 预处理视频...\n');
    [~, video_crc] = preprocess_video(video_path, VIDEO_FRAME_NUM);
    for f = 1:VIDEO_FRAME_NUM
        blk.row = f - 1;
        blk.col = 0;
        blk.total_rows = VIDEO_FRAME_NUM;
        blk.total_cols = 1;
        blk.crc32 = video_crc(f);
        blk.type = 1;
        block_meta = [block_meta, blk];
    end
end

if TX_MODE == 4
    fprintf('[TX-INIT] 准备文本: "%s"\n', TEXT_STRING);
    text_bytes = uint8(unicode2native(TEXT_STRING, 'UTF-8'));
    bytes_per_pkt = 35;
    total_text_pkts = ceil(length(text_bytes) / bytes_per_pkt);
    for p = 1:total_text_pkts
        st = (p-1)*bytes_per_pkt + 1;
        ed = min(p*bytes_per_pkt, length(text_bytes));
        blk.row = p - 1;
        blk.col = 0;
        blk.total_rows = total_text_pkts;
        blk.total_cols = 0;
        blk.crc32 = uint32(0);
        blk.type = 2;
        blk.payload = text_bytes(st:ed);
        block_meta = [block_meta, blk];
    end
end

session_id = 1;
[~, tx_cache] = Data_trans_sig_Gen(Anti_Jamming_Mode, block_meta, [], session_id, TX_MODE);
total_pkts = tx_cache.total_pkt_num;
pkt_acked = false(1, total_pkts);  % ACK跟踪：true=接收端已确认收到

fprintf('[TX-INIT] session=%d | 总块数=%d | CF=%.2f GHz | Gain=%d dB\n', ...
    session_id, total_pkts, CenterFrequency/1e9, Power_gain);

%% ================= 状态机初始化 =================
state = STATE_SENDING;  % 启动即开始发送，不等待握手
sweep_ptr = 1;
round_count = 1;
idle_cnt = 0;
ack_warmup = 0;  % 任务切换后跳过几轮ACK，避免旧session的过期ACK污染

% 反馈链路健康监测
fb_total_attempts = 0;   % 总循环次数
fb_success_count = 0;    % 成功解码的反馈帧数
fb_last_report_idx = 0;  % 上次报告的循环索引
fb_success_rate = 0;     % 反馈链路成功率 (%)

% 任务轮询状态
last_task_version = 0;

%% ================= UI 配置（发射端） =================
tx_ui.enable = true;
tx_ui.url = 'http://127.0.0.1:5001';
tx_ui.health_endpoint = '/api/health';
tx_ui.post_period = 1;   % 每帧发送UI数据，避免IDLE空隙导致频谱显示为直线
tx_ui.ctrl_period = 1;  % 每轮都检查任务切换，SDR硬件缓冲使得开销可忽略
tx_ui.timeout = 1.0;   % JSON大数据需要更长的超时时间
tx_ui = ui_init(tx_ui);
tx_ui.has_bg = true;  % 启用后台异步HTTP，避免webwrite阻塞SDR循环

%% ================= SDR 初始化 =================
radio_tx = comm.SDRuTransmitter('Platform','X310','IPAddress','192.168.10.2');
radio_tx.ChannelMapping = 1;   % 子板1专用于数据链路发射
radio_tx.CenterFrequency = CenterFrequency;
radio_tx.Gain = Power_gain;
radio_tx.MasterClockRate = 200e6;
radio_tx.InterpolationFactor = 512;
radio_tx.ClockSource = 'External';
radio_tx.UnderrunOutputPort = true;

% 注意: EnableDCCorrection/EnableIQCorrection 在当前MATLAB版本不可用
% 将在软件层面进行DC去除

% 反向链路接收（保留硬件初始化，但不依赖其数据做决策）
radio_rx = comm.SDRuReceiver( ...
    'Platform','X310', ...
    'IPAddress','192.168.10.2', ...
    'OutputDataType','double', ...
    'MasterClockRate',200e6, ...
    'DecimationFactor',512, ...
    'SamplesPerFrame',FB_RX_SAMPLES);
radio_rx.ClockSource = 'External';
radio_rx.ChannelMapping = 2;   % RX ch1=Radio#0 RX2端口，信令链路接收（1.45GHz）
radio_rx.CenterFrequency = 1.45e9;
radio_rx.Gain = 15;
radio_rx.OverrunOutputPort = true;

cleanupObj = onCleanup(@() safe_release_txrx(radio_tx, radio_rx));

%% ================= 主循环 =================
for idx = 1:200000
    burst_list = [];
    Par_Rec_signal = complex(zeros(FB_RX_SAMPLES, 1));  % 复数初始化，匹配SDR IQ输出

    % ---------- 状态机 ----------
    switch state
        case STATE_SENDING
            % --- 选择性重传：只发送未ACK的包 ---
            unacked = find(~pkt_acked);
            if isempty(unacked)
                state = STATE_DONE;
                fprintf('[TX-DONE] 所有包已确认，停止发送\n');
                tx_sig = zeros(BUS_SLOT_SAMPLES, 1);
            else
                % 从 sweep_ptr 位置开始找未ACK的包
                cand = unacked(unacked >= sweep_ptr);
                if isempty(cand)
                    % 当前指针后没有未ACK包，回到开头
                    sweep_ptr = unacked(1);
                    round_count = round_count + 1;
                    fprintf('[TX-ROUND] 已完成 %d/%d 轮发送 (剩余%d包)\n', ...
                        round_count-1, NUM_ROUNDS, length(unacked));
                    if round_count > NUM_ROUNDS
                        state = STATE_DONE;
                        fprintf('[TX-DONE] 全部 %d 轮发送完成，停止发射\n', NUM_ROUNDS);
                        tx_sig = zeros(BUS_SLOT_SAMPLES, 1);
                    else
                        cand = unacked(unacked >= sweep_ptr);
                    end
                end
                if state == STATE_SENDING
                    burst_cnt = min(BURST_PKTS, length(cand));
                    burst_list = cand(1:burst_cnt);
                    tx_sig = build_tx_slot(tx_cache, burst_list, BUS_SLOT_SAMPLES, GUARD_PRE, GUARD_BETWEEN, GUARD_POST);
                    sweep_ptr = cand(burst_cnt) + 1;
                    if sweep_ptr > total_pkts
                        sweep_ptr = 1;
                    end
                    state = STATE_IDLE;
                    idle_cnt = IDLE_SLOTS;
                end
            end

        case STATE_IDLE
            tx_sig = zeros(BUS_SLOT_SAMPLES, 1);
            idle_cnt = idle_cnt - 1;
            if idle_cnt <= 0
                state = STATE_SENDING;
            end

        case STATE_DONE
            tx_sig = zeros(BUS_SLOT_SAMPLES, 1);
    end

    tx_sig = sqrt(Power) * 0.5 * tx_sig;

    % 频率偏移 (Low-IF): 将信号搬离 DC，零中频 LO 泄露不再与信号重叠
    t_tx = (0:length(tx_sig)-1)' / samp_rate;
    tx_sig = tx_sig .* exp(1j * 2 * pi * FREQ_OFFSET * t_tx);

    % ---------- 硬件收发 ----------
    try
        tx_underrun = radio_tx(tx_sig);
        [Par_Rec_signal, ~, rx_overrun] = radio_rx();

        if tx_underrun
            warning('[TX-WARN] Underrun');
        end
        if rx_overrun
            % 反向链路 overrun 不影响主链路，静默
        end
    catch ME
        warning('[TX-ERR] 硬件异常：%s', ME.message);
        continue;
    end

    % ---------- 反向链路解析 ----------
    if ~isa(Par_Rec_signal, 'double')
        Par_Rec_signal = double(Par_Rec_signal);
    end
    if isreal(Par_Rec_signal)
        Par_Rec_signal = complex(Par_Rec_signal, 0);
    end
    [Par_Datavalid, session_id_rec, ack_base_rec, ack_bitmap_rec, Anti_Jamming_Mode_new, ~, Frame_type_rec] = ...
        Par_Rece_sig_Gen(Par_Rec_signal);

    % 反馈链路健康统计（仅在发送/空闲状态计数，避免STATE_DONE时稀释）
    if state ~= STATE_DONE
        fb_total_attempts = fb_total_attempts + 1;
        if Par_Datavalid == 1 && Frame_type_rec == 20
            fb_success_count = fb_success_count + 1;
        end
    end
    if mod(idx, 200) == 0 && fb_total_attempts > 0
        fb_success_rate = fb_success_count / fb_total_attempts * 100;
        fprintf('[TX-FB-HEALTH] 反馈链路: 成功率=%.1f%% (%d/%d)\n', ...
            fb_success_rate, fb_success_count, fb_total_attempts);
    end

    if mod(idx, 100) == 0
        fprintf('[TX-REV-DEBUG] idx=%d | Par_Datavalid=%d | Frame_type=%d\n', ...
            idx, Par_Datavalid, Frame_type_rec);
    end

    if Par_Datavalid == 1
        if Frame_type_rec == 20
            % 反向链路反馈: session_id=[Carrier(8)|SNR(8)], ack_base=[Power(8)|Gain(8)]
            rev_carrier_idx = double(bitshift(uint16(session_id_rec), -8));
            rev_snr_byte    = double(bitand(uint16(session_id_rec), uint16(255)));
            rev_snr_db      = rev_snr_byte - 20;
            rev_power_idx   = double(bitshift(uint16(ack_base_rec), -8));
            rev_gain_idx    = double(bitand(uint16(ack_base_rec), uint16(255)));

            % ---- 解析滑动窗口ACK: ack_bitmap = [win_start(16bit) | bitmap(16bit)] ----
            if ack_warmup > 0
                ack_warmup = ack_warmup - 1;
            elseif ack_bitmap_rec > 0 && total_pkts > 0
                % 检测"全部确认"信号: 接收机收齐后发送 0xFFFFFFFF
                if ack_bitmap_rec == uint32(4294967295)
                    pkt_acked(:) = true;
                    state = STATE_DONE;
                    fprintf('[TX-ACK] 收到全部确认信号(0xFFFFFFFF)，%d 包已确认，停止发送\n', total_pkts);
                else
                    ack_win_start = double(bitshift(ack_bitmap_rec, -16));
                    ack_win_bits  = uint16(bitand(ack_bitmap_rec, uint32(65535)));
                    new_acked = 0;
                    for b = 1:16
                        pn = ack_win_start + b;
                        if pn >= 1 && pn <= total_pkts && bitget(ack_win_bits, b) && ~pkt_acked(pn)
                            pkt_acked(pn) = true;
                            new_acked = new_acked + 1;
                        end
                    end
                    if new_acked > 0 && mod(idx, 20) == 0
                        fprintf('[TX-ACK] 窗口%d 新增确认 %d 包 | 总计 %d/%d\n', ...
                            ack_win_start, new_acked, sum(pkt_acked), total_pkts);
                    end
                    % 全部确认则提前终止
                    if all(pkt_acked)
                        state = STATE_DONE;
                        fprintf('[TX-ACK] 所有 %d 包已确认，提前停止发送\n', total_pkts);
                    end
                end
            end

            if mod(idx, 100) == 0
                fprintf('[TX-REV] 反馈: Carrier=%d | Power=%d | Gain=%d | SNR≈%d dB | Mode=%d\n', ...
                    rev_carrier_idx, rev_power_idx, rev_gain_idx, rev_snr_db, Anti_Jamming_Mode_new);
            end

            % ---- 应用参数变更 ----
            if rev_carrier_idx >= 1 && rev_carrier_idx <= length(Carrier_set) ...
               && rev_carrier_idx ~= Carrier_select_rec_bef
                Carrier_select_rec_bef = rev_carrier_idx;
                CenterFrequency = Carrier_set(rev_carrier_idx);
                fprintf('[TX-PAR] 正在切换载频 -> %.2f GHz ...\n', CenterFrequency/1e9);
                radio_tx.CenterFrequency = CenterFrequency;
                fprintf('[TX-PAR] 载频切换完成 -> %.2f GHz\n', CenterFrequency/1e9);
            end

            if rev_power_idx >= 1 && rev_power_idx <= length(Power_set) ...
               && rev_power_idx ~= Trans_power_select_rec
                Trans_power_select_rec = rev_power_idx;
                Power = Power_set(rev_power_idx);
                fprintf('[TX-PAR] 发射功率已更新 -> idx=%d\n', rev_power_idx);
            end

            if rev_gain_idx >= 1 && rev_gain_idx <= length(Power_gain_set) ...
               && rev_gain_idx ~= Power_gain_select_rec_bef
                Power_gain_select_rec_bef = rev_gain_idx;
                Power_gain = Power_gain_set(rev_gain_idx);
                radio_tx.Gain = Power_gain;
                fprintf('[TX-PAR] 增益已更新 -> %d dB\n', Power_gain);
            end

            if Anti_Jamming_Mode_bef ~= Anti_Jamming_Mode_new
                Anti_Jamming_Mode_bef = Anti_Jamming_Mode_new;
                if Anti_Jamming_Mode_bef == 1
                    BURST_PKTS = 3;
                else
                    BURST_PKTS = 10;
                end
                [~, tx_cache] = Data_trans_sig_Gen(Anti_Jamming_Mode_bef, block_meta, [], session_id, TX_MODE);
                total_pkts = tx_cache.total_pkt_num;
                pkt_acked = false(1, total_pkts);
                fprintf('[TX-PAR] Anti_Jamming_Mode=%d | BURST_PKTS=%d\n', Anti_Jamming_Mode_bef, BURST_PKTS);
            end
        end
    end

    % ---------- 参数同步（双通道：推送为主，轮询为备） ----------
    % ---------- 任务轮询 ----------
    if mod(idx, tx_ui.ctrl_period) == 0
        task_update = ui_try_get_task(tx_ui);
        if task_update.needs_update && task_update.version ~= last_task_version
            last_task_version = task_update.version;
            fprintf('[TX-TASK] 检测到任务切换: mode=%d | img=%s | vid=%s | src=%s\n', ...
                task_update.tx_mode, task_update.image_file, task_update.video_file, ...
                task_update.command_source);

            % 重建块数据
            block_meta = rebuild_task_blocks(script_dir, task_update);
            if ~isempty(block_meta)
                TX_MODE = task_update.tx_mode;
                if ~isempty(task_update.image_file)
                    img_path = fullfile(script_dir, task_update.image_file);
                end
                if ~isempty(task_update.video_file)
                    video_path = fullfile(script_dir, task_update.video_file);
                end
                TEXT_STRING = task_update.text_string;

                session_id = session_id + 1;
                [~, tx_cache] = Data_trans_sig_Gen(Anti_Jamming_Mode_bef, block_meta, [], session_id, TX_MODE);
                total_pkts = tx_cache.total_pkt_num;
                pkt_acked = false(1, total_pkts);  % 新任务重置ACK状态

                % 重置传输状态
                sweep_ptr = 1;
                round_count = 1;
                state = STATE_SENDING;
                idle_cnt = 0;
                ack_warmup = 8;  % 冷却期：等待旧session ACK过期

                fprintf('[TX-TASK] 任务重建完成: %d 个包 | session=%d\n', total_pkts, session_id);
            end
        end
    end

    % ---------- UI ----------
    if tx_ui.enable
        % STATE_DONE 降低UI更新频率，避免Flask单线程队列积压导致卡死
        if state == STATE_DONE
            ui_interval = 50;
        else
            ui_interval = tx_ui.post_period;
        end
        if mod(idx, ui_interval) == 0
            tx_payload_ui = build_tx_ui_payload( ...
                tx_sig, Par_Rec_signal, CenterFrequency, Power_gain, samp_rate, ...
                state, session_id, total_pkts, img_path, burst_list, round_count, NUM_ROUNDS, ...
                Carrier_select_rec_bef, Trans_power_select_rec, Power_gain_select_rec_bef, Anti_Jamming_Mode_bef, ...
                fb_success_rate, fb_success_count, fb_total_attempts);
            tx_ui = ui_try_post(tx_ui, '/api/data', tx_payload_ui);
        end
    end

    if mod(idx, 10) == 0
        state_names = {'SENDING', 'IDLE', 'DONE'};
        fprintf('[TX] idx=%d | %s | round=%d/%d | burst=%s | ptr=%d\n', ...
            idx, state_names{state}, round_count, NUM_ROUNDS, ...
            mat2str(burst_list), sweep_ptr);
    end

    if state == STATE_DONE
        % 传输完成，持续轮询等待任务切换（不再退出）
        if mod(idx, 100) == 0
            fprintf('[TX] 传输完成，等待任务切换...\n');
        end
    end
end

release(radio_rx);
release(radio_tx);

%% ================= 局部函数 =================
function sig_out = build_tx_slot(tx_cache, pkt_list, slot_len, guard_pre, guard_between, guard_post)
sig_out = zeros(slot_len, 1);
wr = guard_pre + 1;
for ii = 1:length(pkt_list)
    k = pkt_list(ii);
    one_wave = tx_cache.waveforms{k};
    L = length(one_wave);
    if wr + L - 1 > slot_len - guard_post
        warning('[TX-SLOT] 槽空间不足，%d/%d 个包未装入', length(pkt_list) - ii + 1, length(pkt_list));
        break;
    end
    sig_out(wr:wr + L - 1) = one_wave;
    wr = wr + L + guard_between;
end
end

function safe_release_txrx(tx, rx)
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
disp('发射端 SDR 资源已释放。');
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
    fprintf('[TX-UI] UI 已连接: %s\n', ui.url);
catch
    fprintf('[TX-UI] UI 未连接，不影响主程序: %s\n', ui.url);
end
end

function ctrl = ui_try_get_control(ui)
ctrl = struct('apply', false, 'str', '');
if ~ui.enable
    return;
end
try
    opts = weboptions('Timeout', ui.timeout);
    r = webread([ui.url, '/api/control'], opts);
    if isstruct(r)
        ctrl = r;
    end
catch
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

function data = build_tx_ui_payload(tx_sig, fb_sig, CenterFrequency, Power_gain, samp_rate, ...
    state, session_id, total_pkts, img_file_name, burst_list, round_count, NUM_ROUNDS, ...
    Carrier_select_rec, Trans_power_select_rec, Power_gain_select_rec, Anti_Jamming_Mode_rec, ...
    fb_success_rate, fb_success_count, fb_total_attempts)

N = 2048;
fft_start = max(1, floor(length(tx_sig) / 4));  % 跳过guard_pre保护间隔，取信号实际存在区域
if fft_start + N - 1 > length(tx_sig)
    fft_start = length(tx_sig) - N + 1;
end
fft_segment = tx_sig(fft_start : fft_start + N - 1);
sig_fft = fftshift(fft(fft_segment, N));
f_freq = linspace(-samp_rate / 2, samp_rate / 2, length(sig_fft));
% 发射基带信号已通过频率偏移搬到 +80 kHz，0 Hz 无有效信号，无需 DC 剔除
sig_amp_dB = 20 * log10(abs(sig_fft) / max(abs(sig_fft) + eps) + 1e-10);

td_start = max(1, floor(length(tx_sig) / 5));  % 跳过guard_pre，取信号区域
td_len = length(tx_sig) - td_start + 1;
step_tx = max(1, floor(td_len / 1500));
time_tx = (0:step_tx:td_len-1) / samp_rate * 1000;
amp_tx = abs(tx_sig(td_start:step_tx:end));

time_len_fb = min(3000, length(fb_sig));
time_fb = (0:time_len_fb-1) / samp_rate * 1000;
amp_fb = abs(fb_sig(1:time_len_fb));

if isempty(burst_list)
    burst_text = '[]';
else
    burst_text = mat2str(burst_list);
end

state_names = {'SENDING', 'IDLE', 'DONE'};
state_text = 'UNKNOWN';
if state >= 1 && state <= 3
    state_text = state_names{state};  % MATLAB 1-indexed
end

par_txt = sprintf('Carrier=%d | PowerIdx=%d | GainIdx=%d | Mode=%d', ...
    Carrier_select_rec, Trans_power_select_rec, Power_gain_select_rec, Anti_Jamming_Mode_rec);

data = struct();
data.tx_spec.freq = reshape(f_freq / 1e3, 1, []);
data.tx_spec.amp  = reshape(sig_amp_dB, 1, []);
data.tx_time.time = reshape(time_tx, 1, []);
data.tx_time.amp  = reshape(amp_tx, 1, []);
data.rx_const.i   = [];
data.rx_const.q   = [];
data.rx_time.time = reshape(time_fb, 1, []);
data.rx_time.amp  = reshape(amp_fb, 1, []);

data.status = struct();
data.status.tx_valid = '有效';
data.status.tx_mod = 'QPSK/BPSK自适应';
data.status.tx_mode = sprintf('分块渐进 | %s | session=%d | total=%d | round=%d/%d | burst=%s', ...
    state_text, session_id, total_pkts, round_count, NUM_ROUNDS, burst_text);
data.status.tx_carrier = sprintf('%.2f GHz', CenterFrequency / 1e9);
data.status.tx_samp = sprintf('%.2f kHz', samp_rate / 1e3);
data.status.tx_gain = sprintf('%d dB', Power_gain);
data.status.rx_state = ['反向参数链路: ', par_txt];
data.status.rx_carrier = '1.45 GHz';
data.status.rx_tx_gain = '--';
data.status.rx_tx_carrier = '--';
data.status.fb_health = sprintf('%.1f%% (%d/%d)', fb_success_rate, fb_success_count, fb_total_attempts);
data.status.time = ['更新时间: ', datestr(now, 'HH:MM:SS')];

data.sending_image = img_file_name;
data.sending_file = img_file_name;
end

function task = ui_try_get_task(ui)
task = struct('needs_update', false, 'version', 0, 'tx_mode', 1, ...
    'image_file', 'p2.jpg', 'video_file', '视频.mp4', ...
    'text_string', 'Hello World!', ...
    'command_source', '', 'command_description', '');
if ~ui.enable
    return;
end
try
    opts = weboptions('Timeout', ui.timeout);
    r = webread([ui.url, '/api/tx_decision'], opts);
    if isstruct(r) && isfield(r, 'needs_update') && r.needs_update
        task.needs_update = true;
        task.version = r.decision_version;
        task.tx_mode = r.tx_mode;
        if isfield(r, 'image_file'), task.image_file = r.image_file; end
        if isfield(r, 'video_file'), task.video_file = r.video_file; end
        if isfield(r, 'text_string'), task.text_string = r.text_string; end
        if isfield(r, 'command_source'), task.command_source = r.command_source; end
        if isfield(r, 'command_description'), task.command_description = r.command_description; end
    end
catch
end
end

function block_meta = rebuild_task_blocks(script_dir, task)
IMAGE_GRID_ROWS = 8;
IMAGE_GRID_COLS = 8;
VIDEO_FRAME_NUM = 20;
TX_MODE_LOCAL = task.tx_mode;

block_meta = [];

if ismember(TX_MODE_LOCAL, [1, 3])
    img_path = fullfile(script_dir, task.image_file);
    fprintf('[TX-REBUILD] 预处理图片: %s\n', task.image_file);
    if exist(img_path, 'file')
        [~, img_crc] = preprocess_image(img_path, IMAGE_GRID_ROWS, IMAGE_GRID_COLS);
        for r = 1:IMAGE_GRID_ROWS
            for c = 1:IMAGE_GRID_COLS
                blk.row = r - 1;
                blk.col = c - 1;
                blk.total_rows = IMAGE_GRID_ROWS;
                blk.total_cols = IMAGE_GRID_COLS;
                blk.crc32 = img_crc(r, c);
                blk.type = 0;
                block_meta = [block_meta, blk];
            end
        end
    else
        warning('[TX-REBUILD] 图片文件不存在: %s', img_path);
    end
end

if ismember(TX_MODE_LOCAL, [2, 3])
    video_path = fullfile(script_dir, task.video_file);
    fprintf('[TX-REBUILD] 预处理视频: %s\n', task.video_file);
    if exist(video_path, 'file')
        [~, video_crc] = preprocess_video(video_path, VIDEO_FRAME_NUM);
        for f = 1:VIDEO_FRAME_NUM
            blk.row = f - 1;
            blk.col = 0;
            blk.total_rows = VIDEO_FRAME_NUM;
            blk.total_cols = 1;
            blk.crc32 = video_crc(f);
            blk.type = 1;
            block_meta = [block_meta, blk];
        end
    else
        warning('[TX-REBUILD] 视频文件不存在: %s', video_path);
    end
end

if TX_MODE_LOCAL == 4
    fprintf('[TX-REBUILD] 准备文本: "%s"\n', task.text_string);
    text_bytes = uint8(unicode2native(task.text_string, 'UTF-8'));
    bytes_per_pkt = 35;
    total_text_pkts = ceil(length(text_bytes) / bytes_per_pkt);
    for p = 1:total_text_pkts
        st = (p-1)*bytes_per_pkt + 1;
        ed = min(p*bytes_per_pkt, length(text_bytes));
        blk.row = p - 1;
        blk.col = 0;
        blk.total_rows = total_text_pkts;
        blk.total_cols = 0;
        blk.crc32 = uint32(0);
        blk.type = 2;
        blk.payload = text_bytes(st:ed);
        block_meta = [block_meta, blk];
    end
end
end
