function [img_h, img_w] = get_image_dims(img_path)
info = imfinfo(img_path);
img_h = info.Height;
img_w = info.Width;
end
