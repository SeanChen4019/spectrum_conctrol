function project_setup()
% 干扰模拟控制系统 — MATLAB 项目路径初始化
projectRoot = fileparts(mfilename('fullpath'));
addpath(projectRoot);
addpath(genpath(fullfile(projectRoot, 'backend')));
addpath(genpath(fullfile(projectRoot, 'interface')));
addpath(genpath(fullfile(projectRoot, 'radio')));
addpath(genpath(fullfile(projectRoot, 'sensing')));
addpath(genpath(fullfile(projectRoot, 'waveform')));
disp("干扰模拟控制系统 — MATLAB 路径加载完成。");
end
