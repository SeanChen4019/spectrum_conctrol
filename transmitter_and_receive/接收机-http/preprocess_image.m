function [block_data, block_crc] = preprocess_image(img_path, grid_rows, grid_cols)
% 将图片切分为 grid_rows×grid_cols 块，每块独立压缩为JPEG并计算CRC32
img = imread(img_path);
[img_h, img_w, ~] = size(img);

block_h = floor(img_h / grid_rows);
block_w = floor(img_w / grid_cols);

block_data = cell(grid_rows, grid_cols);
block_crc = zeros(grid_rows, grid_cols, 'uint32');

for r = 1:grid_rows
    for c = 1:grid_cols
        y1 = (r-1)*block_h + 1;
        y2 = r*block_h;
        x1 = (c-1)*block_w + 1;
        x2 = c*block_w;
        block_img = img(y1:y2, x1:x2, :);

        tmp_name = [tempname, '.jpg'];
        imwrite(block_img, tmp_name, 'JPEG');
        fid = fopen(tmp_name, 'rb');
        block_jpeg = fread(fid, inf, 'uint8=>uint8');
        fclose(fid);
        delete(tmp_name);

        block_data{r, c} = block_jpeg;
        block_crc(r, c) = compute_crc32(block_jpeg);
    end
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
