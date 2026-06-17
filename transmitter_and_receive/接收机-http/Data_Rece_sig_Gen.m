function [str_rec,Rec_sig_afr,data_flag,err_valid,err_bit_num,total_num,frame_packets] = Data_Rece_sig_Gen(Anti_Jamming_Mode_select_rec,Trans_sig_data,PER_test,Threshold)
% 合并版：分块渐进式传输 + 多模式抗干扰
% 帧格式（多LDPC块，匹配发射端）：
%   QPSK模式: 5个LDPC块/帧，287字节载荷
%   BPSK模式: 1个LDPC块/帧，44字节载荷（扩频sf=15）
% 载荷头9字节块元数据：row(1) col(1) total_rows(1) total_cols(1) type(1) crc32(4)

defs = link_phy_defs();

persistent rxfilter cfgLDPCDec crcdetector qpskdemod_norm qpskdemod_aj ...
           cfgLDPCEnc_ber crcgenerator_ber ...
           Date_test_ber

Rec_sig_afr = 1;
data_flag = 0;
total_num = 1;
err_valid = 0;
err_bit_num = 0;
str_rec = [];
frame_packets = struct('Session_ID', {}, 'Frame_num', {}, 'Total_frame_num', {}, ...
    'Payload_bytes', {}, 'ZeroPadding_num', {}, 'Payload_length_bits', {}, ...
    'block_row', {}, 'block_col', {}, 'block_total_rows', {}, 'block_total_cols', {}, ...
    'block_crc32', {}, 'block_type', {}, 'tx_mode', {});

sps = 4;
maxnumiter = 10;

%% ============== 持久化系统对象 ==============
if isempty(rxfilter)
    rxfilter = comm.RaisedCosineReceiveFilter('InputSamplesPerSymbol', sps, 'DecimationFactor', 1, 'RolloffFactor', 0.25);

    pcmatrix = ldpcQuasiCyclicMatrix(defs.blockSize, defs.P);
    cfgLDPCDec = ldpcDecoderConfig(pcmatrix);

    crcdetector = comm.CRCDetector(defs.poly);

    qpskdemod_norm = comm.PSKDemodulator(4, 'BitOutput', true, 'DecisionMethod', 'Approximate log-likelihood ratio');
    qpskdemod_norm.PhaseOffset = pi/4;

    qpskdemod_aj = comm.PSKDemodulator(2, 'BitOutput', true, 'DecisionMethod', 'Approximate log-likelihood ratio');
    qpskdemod_aj.PhaseOffset = pi;

    cfgLDPCEnc_ber = ldpcEncoderConfig(pcmatrix);
    crcgenerator_ber = comm.CRCGenerator(defs.poly);
end

%% ============== 模式选择 ==============
if Anti_Jamming_Mode_select_rec == 1
    M = 2;
    sf = 15;
    qpskdemod = qpskdemod_aj;
    LDPC_BLOCKS = 1;
    pay_load_length_num = 44;
    Data_frame_len = LDPC_BLOCKS * 648 / log2(M) * sf;  % = 9720 符号
else
    M = 4;
    qpskdemod = qpskdemod_norm;
    LDPC_BLOCKS = 1;             % 匹配发射端：1块/帧, 44字节载荷
    pay_load_length_num = 44;
    Data_frame_len = LDPC_BLOCKS * 648 / log2(M);  % = 324 符号
end

%% ============== BER测试参考信号生成 ==============
if PER_test == 1 && isempty(Date_test_ber)
    str_ori = '误码率测试';
    utf8_bytes = unicode2native(str_ori, 'UTF-8');
    bin_chars = dec2bin(utf8_bytes, 8);
    bit_char_array = reshape(bin_chars', 1, []);
    logic_arr = logical(bit_char_array == '1');
    Source_Data0 = double(logic_arr');

    Total_frame_num_max = 1;
    if length(Source_Data0) > Total_frame_num_max * pay_load_length_num * 8
        Source_Data0 = Source_Data0(1:Total_frame_num_max*pay_load_length_num*8);
    end
    Total_frame_num_temp = ceil(length(Source_Data0)/pay_load_length_num/8);
    Total_frame_num_temp = min(Total_frame_num_temp, Total_frame_num_max);
    zeropadding_num = Total_frame_num_temp*pay_load_length_num*8 - length(Source_Data0);

    zeropadding_num_trans = double(logical(dec2bin(zeropadding_num, 9) == '1'))';
    Source_Data = [Source_Data0; zeros(zeropadding_num, 1)];
    Total_frame_num = double(logical(dec2bin(Total_frame_num_temp, 16) == '1'))';

    Frame_num_temp = 1;
    Frame_num = double(logical(dec2bin(Frame_num_temp, 16) == '1'))';
    if Total_frame_num_temp == 1
        Payload_length = double(logical(dec2bin(length(Source_Data), 16) == '1'));
    elseif Frame_num_temp < Total_frame_num_temp
        Payload_length = double(logical(dec2bin(pay_load_length_num*8, 16) == '1'));
    else
        Payload_length_temp = length(Source_Data) - (Frame_num_temp-1)*pay_load_length_num*8;
        Payload_length = double(logical(dec2bin(Payload_length_temp, 16) == '1'));
    end
    Payload_length = Payload_length';
    Payload = Source_Data((Frame_num_temp-1)*pay_load_length_num*8+1 : Frame_num_temp*pay_load_length_num*8);
    session_ID_ber = double(logical(dec2bin(1, 16) == '1'))';
    Payload_frame_temp = [defs.frame_head; defs.user_id; defs.data_frame_type; session_ID_ber; Total_frame_num; ...
                          Frame_num; Payload_length; zeropadding_num_trans; Payload];
    encData = crcgenerator_ber(Payload_frame_temp);
    Payload_frame = [encData; defs.data_frame_end];
    Date_test_ber = Payload_frame;
end

%% ============收端匹配滤波=================
try
    Rec_sig = rxfilter(Trans_sig_data);
catch ME
    if contains(ME.message, '复/实性') || contains(ME.message, 'complexity')
        release(rxfilter);
        rxfilter = comm.RaisedCosineReceiveFilter('InputSamplesPerSymbol', sps, 'DecimationFactor', 1, 'RolloffFactor', 0.25);
        Rec_sig = rxfilter(Trans_sig_data);
    else
        rethrow(ME);
    end
end

%% ============同步=========================
data_sys = [];
index_val = zeros(1, sps);
index_loc_h = cell(1, sps);
syn_flag = 0;

for i = 1:sps
    data_sys(:, i) = Rec_sig(i:sps:end);
    buffer_h = abs(conv(flip(defs.head_data), sign(data_sys(:, i))));
    cand = pick_sync_peaks(buffer_h, Threshold);

    if ~isempty(cand)
        syn_flag = 1;
        index_loc_h{i} = cand(:);
        index_val(i) = mean(buffer_h(cand));
    else
        index_loc_h{i} = [];
    end
end

if syn_flag == 0
    return;
end

[~, op_index] = max(index_val);
Rec_sig_afr_temp = data_sys(:, op_index);
index_start_temp = index_loc_h{op_index};

if isempty(index_start_temp)
    return;
end

if index_start_temp(end) + Data_frame_len > length(Rec_sig_afr_temp)
    index_start_temp = index_start_temp(index_start_temp + Data_frame_len <= length(Rec_sig_afr_temp));
end

if isempty(index_start_temp)
    return;
end

seen_frames = [];

for j = 1:length(index_start_temp)
    index_start = index_start_temp(j);

    if index_start < 512
        continue;
    end
    if index_start + Data_frame_len > length(Rec_sig_afr_temp)
        continue;
    end

    train_len = min(511, index_start);
    receive_train_seq_tem = Rec_sig_afr_temp(index_start - train_len + 1 : index_start);
    desire_seq = defs.head_data(end - train_len + 1 : end);
    temp = conj(desire_seq) .* receive_train_seq_tem;
    phase_est = -angle(mean(temp));
    Rec_sig_afr = Rec_sig_afr_temp(index_start+1 : index_start+Data_frame_len) .* exp(1j*phase_est);

    %% ==============解调==========================
    demodSignal = qpskdemod(Rec_sig_afr);

    %% =============解扩（仅BPSK模式）==================
    if Anti_Jamming_Mode_select_rec == 1
        data_desp = zeros(length(demodSignal)/sf, 1);
        for i_desp = 1:length(demodSignal)/sf
            data_desp(i_desp) = sum(demodSignal((i_desp-1)*sf+1 : i_desp*sf) .* defs.pn_data);
        end
    else
        data_desp = demodSignal;
    end

    %% ============解交织 + 信道译码（多LDPC块）===========
    coded_bits_per_block = 648;
    receivedBits_all = zeros(LDPC_BLOCKS * 486, 1);
    for b = 1:LDPC_BLOCKS
        idx_start = (b-1) * coded_bits_per_block + 1;
        idx_end = b * coded_bits_per_block;
        block_bits = data_desp(idx_start:idx_end);

        deinter_matrix = reshape(block_bits, 18, 36);
        deinter_matrix = deinter_matrix';
        de_interleaved_data = deinter_matrix(:);

        decoded_block = ldpcDecode(de_interleaved_data, cfgLDPCDec, maxnumiter);
        receivedBits_all((b-1)*486+1 : b*486) = decoded_block;
    end

    %% ===============解扰==========================
    deScrData = descramble_bits(receivedBits_all, defs.scr_seq);

    if PER_test == 1
        if length(deScrData) == length(Date_test_ber)
            [c_err, ~] = find(deScrData ~= Date_test_ber);
            err_bit_num = length(c_err);
            total_num = length(deScrData);
            err_valid = 1;
        end
    end

    %% =====================信宿解析=======================
    [data_rec, err] = crcdetector(deScrData(1:end-length(defs.data_frame_end)));

    if err ~= 0
        continue;
    end

    rx_tx_mode = bits_to_int(data_rec(8+1 : 8+8));  % user_id字段现承载tx_mode
    session_id = bits_to_int(data_rec(8+8+8+1 : 8+8+8+16));
    Total_frame_num_rec = bits_to_int(data_rec(8+8+8+16+1 : 8+8+8+16+16));
    Frame_num_rec = bits_to_int(data_rec(8+8+8+16+16+1 : 8+8+8+16+16+16));
    Payload_length_rec = bits_to_int(data_rec(8+8+8+16+16+16+1 : 8+8+8+16+16+16+16));
    ZeroPadding_num_rec = bits_to_int(data_rec(8+8+8+16+16+16+16+1 : 8+8+8+16+16+16+16+9));

    payload_bits_all = data_rec(8+8+8+16+16+16+16+9+1 : 8+8+8+16+16+16+16+9+pay_load_length_num*8);
    payload_bits = payload_bits_all(1:Payload_length_rec);

    if isempty(payload_bits) || mod(length(payload_bits), 8) ~= 0
        continue;
    end

    payload_bytes = bits_to_bytes(payload_bits);

    if any(seen_frames == Frame_num_rec)
        continue;
    end
    seen_frames(end+1) = Frame_num_rec; %#ok<AGROW>

    one_pkt.Session_ID = session_id;
    one_pkt.Frame_num = Frame_num_rec;
    one_pkt.Total_frame_num = Total_frame_num_rec;
    one_pkt.Payload_bytes = payload_bytes;
    one_pkt.ZeroPadding_num = ZeroPadding_num_rec;
    one_pkt.Payload_length_bits = Payload_length_rec;
    one_pkt.tx_mode = rx_tx_mode;  % 发射端的任务模式(1-4)

    % 解析块元数据: row(1) col(1) total_rows(1) total_cols(1) type(1) crc32(4)
    if length(payload_bytes) >= 9
        one_pkt.block_row = double(payload_bytes(1));
        one_pkt.block_col = double(payload_bytes(2));
        one_pkt.block_total_rows = double(payload_bytes(3));
        one_pkt.block_total_cols = double(payload_bytes(4));
        one_pkt.block_type = double(payload_bytes(5));
        one_pkt.block_crc32 = typecast(payload_bytes(6:9), 'uint32');
    else
        one_pkt.block_row = -1;
        one_pkt.block_col = -1;
        one_pkt.block_total_rows = 0;
        one_pkt.block_total_cols = 0;
        one_pkt.block_type = -1;
        one_pkt.block_crc32 = uint32(0);
    end

    frame_packets(end+1) = one_pkt; %#ok<AGROW>
    str_rec = payload_bytes;
    data_flag = 1;
end

end

%% ================== 局部辅助函数 ==================
function cand = pick_sync_peaks(metric, thr)
raw_idx = find(metric >= thr);
cand = [];
if isempty(raw_idx)
    return;
end

group_gap = 20;
st = 1;
while st <= length(raw_idx)
    ed = st;
    while ed < length(raw_idx) && (raw_idx(ed+1) - raw_idx(ed)) <= group_gap
        ed = ed + 1;
    end
    group = raw_idx(st:ed);
    [~, loc] = max(metric(group));
    cand(end+1,1) = group(loc); %#ok<AGROW>
    st = ed + 1;
end
end

function out = descramble_bits(in, scr_seq)
out = zeros(size(in));
grp = length(scr_seq);
for ii = 1:length(in)/grp
    st = (ii-1)*grp + 1;
    ed = ii*grp;
    out(st:ed) = xor(in(st:ed), scr_seq);
end
end

function v = bits_to_int(bits)
v = (2.^(length(bits)-1:-1:0)) * bits(:);
end

function u8 = bits_to_bytes(bits)
bit_str = char(bits(:).' + '0');
bin_chars = reshape(bit_str, 8, []).';
u8 = uint8(bin2dec(bin_chars));
end
