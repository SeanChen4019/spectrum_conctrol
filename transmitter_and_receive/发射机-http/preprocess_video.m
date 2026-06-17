function [frame_data, frame_crc] = preprocess_video(video_path, frame_count)
% 从视频中均匀抽取 frame_count 帧，缩略图压缩为JPEG并计算CRC32
v = VideoReader(video_path);
total_frames = v.NumFrames;
if isinf(total_frames) || total_frames < 1
    total_frames = v.Duration * v.FrameRate;
end
total_frames = max(1, floor(total_frames));

frame_indices = round(linspace(1, total_frames, frame_count));

frame_data = cell(frame_count, 1);
frame_crc = zeros(frame_count, 1, 'uint32');

for i = 1:frame_count
    try
        v.CurrentTime = (frame_indices(i) - 1) / v.FrameRate;
        f = readFrame(v);
    catch
        f = zeros(120, 160, 3, 'uint8');
    end

    f = imresize(f, [120, 160]);

    tmp_name = [tempname, '.jpg'];
    imwrite(f, tmp_name, 'JPEG');
    fid = fopen(tmp_name, 'rb');
    frame_jpeg = fread(fid, inf, 'uint8=>uint8');
    fclose(fid);
    delete(tmp_name);

    frame_data{i} = frame_jpeg;
    frame_crc(i) = compute_crc32(frame_jpeg);
end
end

function crc_val = compute_crc32(data_bytes)
poly = uint32(hex2dec('EDB88320'));
crc = uint32(hex2dec('FFFFFFFF'));
for i = 1:length(data_bytes)
    crc = bitxor(crc, uint32(data_bytes(i)));
    for j = 1:8
        if bitand(crc, 1)
            crc = bitxor(bitshift(crc, -1), poly);
        else
            crc = bitshift(crc, -1);
        end
    end
end
crc_val = bitxor(crc, uint32(hex2dec('FFFFFFFF')));
end
