---
name: ssh-access
description: >
  SSH access to test machines through bastion host. REQUIRED before using
  ssh_connect — contains bastion host address, target machine IP list,
  hardware token workflow, and access types (ssh/wcssh/direct). Read this
  skill first when connecting to any test/dev machine.
version: 2.0.0
metadata:
  tags: [ssh, bastion, remote, access]
---

# SSH 远程访问

## 前置信息

- **堡垒机配置**：`assets/bastion.json`（host、port、username）
- **目标机清单**：`assets/targets.json`（别名 → host/access/via/run_as；access=ssh+sudo 的机器带 `stateful: true` + `switch_chain`，wcsssh 容器不带——它进容器即 app，不用 sudo）


## 步骤 1：连堡垒机（mode="shell"，必须！）

读 `assets/bastion.json` 获取 host/username，然后：
```
ssh_connect(name="bastion", host="<bastion.json host>", user="<bastion.json username>", mode="shell", token="<用户提供的Token>")
```

**必须 mode="shell"**：堡垒机用一次性 Token，exec 探测会浪费 Token。
Token 有效期很短，**连接前先确认好 mode 和参数，一次成功**。
如果用户没给 Token，ssh_connect 会自动问。

## 步骤 2：在目标机执行命令

读 `assets/targets.json` 查目标机的 access 类型：

### access="ssh"（**全部 stateful**：进目标机 → sudo su → 裸命令 → exit）

**所有 access="ssh" 的机器都不能用 `sudo su - <user> -c '<cmd>'` 一次性形式**——sudoers 免密只认交互式 `sudo su - <user>`，带 `-c` 会触发 `[sudo] password for <user>` / `sudo: no tty present` → 失败（stateful 多步 su 机器实测中招）。

必须用「进入→切换→执行→退出」的 stateful 模式（类似 wcsssh 容器），全程 `session="bastion"`、**不带 target_ip**（命令直接发到堡垒机 shell，逐层进入）。按 targets.json 的 `switch_chain` 依次 su：

1. **进目标机**：`ssh_exec(session="bastion", command="ssh <host>")` → 等 `[<user>@<host> ~]$`
2. **按 switch_chain 依次切用户**（每步免密、等 prompt 变到目标用户）：
   - 多数机器 `switch_chain=["app"]`：`sudo su - app` → `[app@...]$`
   - 多步机器 `switch_chain=["hadoop","<目标用户>"]`：先 `sudo su - hadoop`，再 `sudo su - <目标用户>`
3. **执行命令**（此后都在最终用户下，**裸命令、不用再切**）：`ssh_exec(session="bastion", command="tail -200 /path/to/log")`
4. **逐层退出**：switch_chain 有 N 个用户就 `exit` N 次（退回登录用户），再 `exit` 一次退目标机回堡垒机（共 N+1 次）。

例（stateful 机器，switch_chain=["app"]，host=<目标机IP>）：
```
ssh_exec(session="bastion", command="ssh <目标机IP>")
ssh_exec(session="bastion", command="sudo su - app")          # 免密切 app
ssh_exec(session="bastion", command="tail -200 /data/app/logs/app.log")   # app 下裸命令
ssh_exec(session="bastion", command="exit")                   # app → <user>
ssh_exec(session="bastion", command="exit")                   # 目标机 → 堡垒机
```

第 3 步起每条命令都在同一个 login shell 里（cwd、环境变量跨命令共享），**不要每条都重新 ssh/su**；要换机器或结束，先 `exit` 退回堡垒机。

### access="wcsssh"（容器类服务：db、front）

**重要：wcsssh 不能带命令参数！它只负责进入容器。** 必须分步：

1. **进入容器**：
```
ssh_exec(session="bastion", command="wcsssh <容器机IP>")
```
等待容器 prompt 出现（类似 `[app@xxx data]$`）。

2. **在容器内执行命令**：
```
ssh_exec(session="bastion", command="ps aux | grep java | grep -v grep")
```

3. **退出容器**：
```
ssh_exec(session="bastion", command="exit")
```

进入后已是 app 用户，不需要 sudo su。

> **wcsssh 本身就是有状态的**（和 access=ssh 的 stateful 同一个"进→跑→退出"模式）：进容器一次后，后续 `ssh_exec(session="bastion", command="…")` 都在同一个容器 shell 里跑（**不要每条都重新 wcsssh**），直到 `exit` 退出。区别只是 wcsssh 进去直接是 app、不用 `sudo su`，所以 targets.json 里**不打 `stateful` 标志**——那个标志（+`switch_chain`）只给 ssh+sudo 机器用。

### access="direct"（直连，不走堡垒机）
单独 ssh_connect 到该机器，然后 ssh_exec：
```
ssh_connect(name="my-server", host="192.168.1.100", user="admin", password="<密码>")
ssh_exec(session="my-server", command="ls -la")
```

## Session 保存/恢复

- ssh_connect 成功后参数自动保存（不含密码）
- gateway 重启后 ssh_status 显示 saved sessions（alive=false）
- 重连只需 ssh_connect(name="xxx")，参数自动填充，只问密码/Token

## 常用命令

| 场景 | 命令 |
|------|------|
| 看日志 | `tail -200 /path/to/app.log` |
| 搜日志 | `grep -n "ERROR" /path/to/*.log \| tail -50` |
| 看进程 | `ps aux \| grep java` |
| 看端口 | `netstat -tlnp \| grep 8080` |
| 看磁盘 | `df -h` |
| 看负载 | `uptime` |
| 看内存 | `free -h` |

## 注意

- Token 有效期短：认证失败 → 重新 ssh_connect
- wcsssh 不能带命令参数，分步操作：先 `wcsssh <容器机IP>` 进容器，再执行命令，最后 `exit` 退出
- **所有 access="ssh" 机器都不能用 `-c` 一次性 sudo**（触发 `[sudo] password` / `no tty`），必须 stateful：`ssh` 进目标机 → 按 `switch_chain` 依次 `sudo su - <user>`（免密）→ 裸命令 → `exit`×(switch_chain 长度+1)
- **不要主动 ssh_disconnect**：保持会话活跃，后续操作可复用同一连接，避免重复 Token 认证（Token 是一次性的，断开后重连需要新 Token）。仅在用户明确要求时才 disconnect
