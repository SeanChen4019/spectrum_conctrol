function Trans_sig = Par_trans_sig_Gen(mode_or_anti_jam, varargin)
% 统一参数/反馈传输函数
% 参数信道模式 (QPSK, 高速):
%   Par_trans_sig_Gen(Anti_Jamming_Mode, Carrier_select, Trans_power_select, Power_gain_select)
% 反向链路反馈模式 (BPSK+扩频sf=15, 高可靠):
%   Par_trans_sig_Gen('feedback', Frame_type, session_id, ack_base, ack_bitmap, Anti_Jamming_Mode)

defs = link_phy_defs();
sps = 4;

persistent txfilter_qpsk txfilter_bpsk cfgLDPCEnc crcgenerator qpskmod bpskmod ...
           scr_seq

if isempty(txfilter_qpsk)
    pcmatrix = ldpcQuasiCyclicMatrix(defs.blockSize, defs.P);
    cfgLDPCEnc = ldpcEncoderConfig(pcmatrix);
    crcgenerator = comm.CRCGenerator(defs.poly);

    txfilter_qpsk = comm.RaisedCosineTransmitFilter('OutputSamplesPerSymbol', sps, 'RolloffFactor', 0.25);
    txfilter_bpsk = comm.RaisedCosineTransmitFilter('OutputSamplesPerSymbol', sps, 'RolloffFactor', 0.25);

    qpskmod = comm.PSKModulator(4, 'BitInput', true);
    qpskmod.PhaseOffset = pi/4;

    bpskmod = comm.PSKModulator(2, 'BitInput', true);
    bpskmod.PhaseOffset = pi/4;

    scr_seq = defs.scr_seq;
end

%% ==================== 模式分发 ====================
if ischar(mode_or_anti_jam) && strcmp(mode_or_anti_jam, 'feedback')
    % ======== 反向链路反馈模式 (BPSK + sf=15扩频) ========
    if length(varargin) >= 4
        Frame_type_temp = varargin{1};
        session_id_temp = varargin{2};
        ack_base_temp = varargin{3};
        ack_bitmap_temp = varargin{4};
    else
        Frame_type_temp = 20; session_id_temp = 1;
        ack_base_temp = 0; ack_bitmap_temp = uint32(0);
    end
    if length(varargin) >= 5
        Anti_Jamming_Mode_select_temp = varargin{5};
    else
        Anti_Jamming_Mode_select_temp = 0;
    end

    ack_bitmap_temp = uint32(ack_bitmap_temp);
    bitmap_hi = bitand(bitshift(ack_bitmap_temp, -16), uint32(65535));
    bitmap_lo = bitand(ack_bitmap_temp, uint32(65535));

    payload_bits = [ ...
        defs.frame_head; ...
        defs.user_id; ...
        bits_from_int(Frame_type_temp, 8); ...
        bits_from_int(session_id_temp, 16); ...
        bits_from_int(ack_base_temp, 16); ...
        bits_from_int(double(bitmap_hi), 16); ...
        bits_from_int(double(bitmap_lo), 16); ...
        bits_from_int(Anti_Jamming_Mode_select_temp, 8)];

    coded_in = [crcgenerator(payload_bits); defs.fb_frame_end];
    scr_bits = scramble_bits(coded_in, scr_seq);
    enc_bits = ldpcEncode(scr_bits, cfgLDPCEnc);

    inter_matrix = reshape(enc_bits, 36, 18).';
    inter_bits = inter_matrix(:);

    inter_polar = 2 * inter_bits - 1;
    spread_seq = zeros(length(inter_polar) * 15, 1);
    for ii = 1:length(inter_polar)
        spread_seq((ii-1)*15+1 : ii*15) = inter_polar(ii) * defs.pn_fb;
    end

    mod_signal = bpskmod(0.5 * (spread_seq + 1));
    tx_in = [defs.head_fb; mod_signal; zeros(sps*10, 1)];
    Trans_sig = txfilter_bpsk(tx_in);
    Trans_sig = [zeros(2000, 1); Trans_sig];

else
    % ======== 参数信道模式 (QPSK, 高速) ========
    Anti_Jamming_Mode_select_temp = mode_or_anti_jam;
    if length(varargin) >= 3
        Carrier_select_temp = varargin{1};
        Trans_power_select_temp = varargin{2};
        Power_gain_select_temp = varargin{3};
    else
        Carrier_select_temp = 3;
        Trans_power_select_temp = 7;
        Power_gain_select_temp = 15;
    end

    session_ID_temp = 1;
    session_ID = double(logical(dec2bin(session_ID_temp, 16) == '1'))';
    Carrier_select = double(logical(dec2bin(Carrier_select_temp, 16) == '1'))';
    Trans_power_select = double(logical(dec2bin(Trans_power_select_temp, 16) == '1'))';
    Power_gain_select = double(logical(dec2bin(Power_gain_select_temp, 16) == '1'))';
    Anti_Jamming_Mode_select = double(logical(dec2bin(Anti_Jamming_Mode_select_temp, 8) == '1'))';

    Payload_frame_temp = [defs.frame_head; defs.user_id; defs.data_frame_type; session_ID; ...
                          Carrier_select; Trans_power_select; Power_gain_select; Anti_Jamming_Mode_select];
    encData = crcgenerator(Payload_frame_temp);
    Payload_frame = [encData; defs.param_frame_end];  % 128 + 358 = 486 bit

    temp_seq = zeros(1, length(Payload_frame));
    for i = 1:length(Payload_frame)/length(scr_seq)
        temp_seq((i-1)*length(scr_seq)+1 : i*length(scr_seq)) = ...
            xor(Payload_frame((i-1)*length(scr_seq)+1 : i*length(scr_seq)), scr_seq);
    end
    ScrData = temp_seq;

    encodedData = ldpcEncode(ScrData', cfgLDPCEnc);

    inter_matrix = reshape(encodedData, 36, 18);
    inter_matrix = inter_matrix';
    interleaved_data = inter_matrix(:);

    modSignal = qpskmod(interleaved_data);
    data_bef_rrc = [defs.head_data; modSignal; zeros(sps*10, 1)];
    Trans_sig = txfilter_qpsk(data_bef_rrc);
    Trans_sig = [zeros(2000, 1); Trans_sig];
end

end

%% ================== 局部辅助函数 ==================
function bits = bits_from_int(v, width)
bits = double(dec2bin(max(0, v), width) == '1').';
end

function out = scramble_bits(in, scr_seq)
out = zeros(size(in));
grp = length(scr_seq);
for ii = 1:length(in)/grp
    st = (ii-1)*grp + 1;
    ed = ii*grp;
    out(st:ed) = xor(in(st:ed), scr_seq);
end
end
