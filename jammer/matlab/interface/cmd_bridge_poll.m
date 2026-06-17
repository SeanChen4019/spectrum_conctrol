function commands = cmd_bridge_poll(client)
% cmd_bridge_poll  轮询Python命令桥接，获取待执行的控制命令
%   commands = cmd_bridge_poll(client)
%   返回一个cell数组的命令结构体，如果没有待处理命令则返回空cell。

    commands = {};
    try
        % 发送轮询请求
        poll_msg = jsonencode(struct("type", "cmd_poll"));
        writeline(client, poll_msg);

        % 读取响应（非阻塞，超时后返回空）
        if client.NumBytesAvailable > 0
            rsp_str = readline(client);
            rsp = jsondecode(rsp_str);
            if strcmp(rsp.type, "cmd_rsp") && ~isempty(rsp.commands)
                cmds = rsp.commands;
                if isstruct(cmds)
                    cmds = num2cell(cmds);
                end
                commands = cmds;
            end
        end
    catch ME
        % 连接断开会抛出异常 - 静默处理
        if ~strcmp(ME.identifier, "transport:client:connectFailed")
            disp("[CMD] 轮询警告: " + ME.message);
        end
    end
end
