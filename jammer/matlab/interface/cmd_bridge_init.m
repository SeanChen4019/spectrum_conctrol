function client = cmd_bridge_init(host, port, timeout_sec)
if nargin < 1, host = "127.0.0.1"; end
if nargin < 2, port = 5557; end
if nargin < 3, timeout_sec = 1; end
client = tcpclient(host, port, "Timeout", timeout_sec, "ConnectTimeout", 5);
disp("[CMD] 已连接到命令桥接 " + host + ":" + string(port));
end
