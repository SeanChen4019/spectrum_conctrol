function [datavalid, session_id_rec, ack_base_rec, ack_bitmap_rec, Anti_Jamming_Mode_select_rec, Rec_sig_afr, Frame_type_rec] = Par_Rece_sig_Gen(Rec_sig0)
% 高速可靠传输版反馈接收函数（persistent版：避免每帧重建System object）

datavalid = 0;
session_id_rec = 0;
ack_base_rec = 1;
ack_bitmap_rec = uint32(0);
Anti_Jamming_Mode_select_rec = 0;
Rec_sig_afr = 0;
Frame_type_rec = 0;

defs = link_phy_defs();
sps = 4;
Threshold = 120;  % 降低门限以适配弱信号反馈链路

persistent rxfilter cfgLDPCDec crcdetector demodulator debug_cnt input_is_complex

if isempty(rxfilter)
    pcmatrix = ldpcQuasiCyclicMatrix(defs.blockSize, defs.P);
    cfgLDPCDec = ldpcDecoderConfig(pcmatrix);
    crcdetector = comm.CRCDetector(defs.poly);

    demodulator = comm.PSKDemodulator(2, 'BitOutput', true, 'DecisionMethod', 'Approximate log-likelihood ratio');
    demodulator.PhaseOffset = pi/4;

    rxfilter = comm.RaisedCosineReceiveFilter( ...
        'InputSamplesPerSymbol', sps, ...
        'DecimationFactor', 1, ...
        'RolloffFactor', 0.25);

    debug_cnt = 0;
    input_is_complex = true;  % 默认复数输入
end

debug_cnt = debug_cnt + 1;

% 输入复/实性变化时，释放并重建滤波器，避免System object类型锁定报错
cur_is_complex = ~isreal(Rec_sig0);
if cur_is_complex ~= input_is_complex
    release(rxfilter);
    rxfilter = comm.RaisedCosineReceiveFilter( ...
        'InputSamplesPerSymbol', sps, ...
        'DecimationFactor', 1, ...
        'RolloffFactor', 0.25);
    input_is_complex = cur_is_complex;
end

Rec_sig = rxfilter(Rec_sig0);
data_sys = [];
buffer_h = [];
index_val = zeros(1, sps);
index_loc_h = cell(1, sps);
syn_flag = 0;

data_frame_len = 648 * 15;

for i = 1:sps
    data_sys(:, i) = Rec_sig(i:sps:end);
    buffer_h(:, i) = abs(conv(flip(defs.head_fb), sign(data_sys(:, i))));
    cand = pick_sync_peaks(buffer_h(:, i), Threshold);

    if ~isempty(cand)
        syn_flag = 1;
        index_loc_h{i} = cand(:);
        index_val(i) = mean(buffer_h(cand, i));
    else
        index_loc_h{i} = [];
    end
end

if mod(debug_cnt, 100) == 0
    max_corr = max(buffer_h(:));
    fprintf('[PAR-RX-DEBUG] 第%d次调用 | max_corr=%.1f | Threshold=%d | syn_flag=%d | sig_power=%.4f\n', ...
        debug_cnt, max_corr, Threshold, syn_flag, mean(abs(Rec_sig0).^2));
end

if syn_flag == 0
    return;
end

[~, op_index] = max(index_val);
Rec_sig_afr_temp = data_sys(:, op_index);
index_start_temp = index_loc_h{op_index};
index_start_temp = index_start_temp(index_start_temp + data_frame_len <= length(Rec_sig_afr_temp));

if isempty(index_start_temp)
    return;
end

for j = 1:length(index_start_temp)
    index_start = index_start_temp(j);

    train_len = min(1023, index_start);
    receive_train_seq_tem = Rec_sig_afr_temp(index_start-train_len+1:index_start);
    desire_seq = defs.head_fb(end-train_len+1:end);
    temp = conj(desire_seq) .* receive_train_seq_tem;
    phase_est = -angle(mean(temp));

    Rec_sig_afr = Rec_sig_afr_temp(index_start+1:index_start+data_frame_len) .* exp(1j * phase_est);
    demod_signal = demodulator(Rec_sig_afr);

    data_desp = zeros(length(demod_signal)/15, 1);
    for ii = 1:length(demod_signal)/15
        data_desp(ii) = sum(demod_signal((ii-1)*15+1 : ii*15) .* defs.pn_fb);
    end

    deinter_matrix = reshape(data_desp, 18, 36).';
    de_interleaved_data = deinter_matrix(:);
    received_bits = ldpcDecode(de_interleaved_data, cfgLDPCDec, 10);
    de_scr_data = descramble_bits(received_bits, defs.scr_seq);

    [data_rec, err] = crcdetector(de_scr_data(1:end-length(defs.fb_frame_end)));
    if err ~= 0
        continue;
    end

    Frame_type_rec = bits_to_int(data_rec(8+8+1 : 8+8+8));
    session_id_rec = bits_to_int(data_rec(8+8+8+1 : 8+8+8+16));
    ack_base_rec = bits_to_int(data_rec(8+8+8+16+1 : 8+8+8+16+16));
    bitmap_hi = bits_to_int(data_rec(8+8+8+16+16+1 : 8+8+8+16+16+16));
    bitmap_lo = bits_to_int(data_rec(8+8+8+16+16+16+1 : 8+8+8+16+16+16+16));
    Anti_Jamming_Mode_select_rec = bits_to_int(data_rec(8+8+8+16+16+16+16+1 : 8+8+8+16+16+16+16+8));

    ack_bitmap_rec = bitor(bitshift(uint32(bitmap_hi), 16), uint32(bitmap_lo));
    datavalid = 1;
    return;
end

end

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
