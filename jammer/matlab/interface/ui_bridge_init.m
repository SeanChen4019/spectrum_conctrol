function client = ui_bridge_init(host, port, timeout_sec)
if nargin < 1, host = "127.0.0.1"; end
if nargin < 2, port = 5555; end
if nargin < 3, timeout_sec = 1; end
client = tcpclient(host, port, "Timeout", timeout_sec);
disp("已连接 UI 遥测服务。");
end
