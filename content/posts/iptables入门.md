---
date: 2026-02-18T18:20:14+08:00
title: iptables入门
tags: [网络,iptables]
categories: [网络]
draft: false
---

## 基础概念

iptables 是 Linux 系统中一个防火墙管理工具，真正实现防火墙功能的是位于内核的 netfilter，我们配置了 iptables 规则后 Netfilter 通过这些规则来进行防火墙过滤等操作。防火墙工作在 OSI 中的第 4 层。它控制网络数据包的进出、转发和修改。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/1479220-20180926145432701-774427524.png)

从图中可以看到，内核在 IP 协议栈的关键位置注册了若干 **hook 点**，每个 hook 都可以挂载规则链。数据包流经路径时，会依次经过这些 hook。

要理解 iptables，最重要的是掌握其 **“四表五链”** 的架构。

### 五链

链是在某个 hook 点执行的一组规则，链决定了在数据包传输的哪个阶段触发规则。

Linux 内核在 IP 协议栈关键位置定义了五个 hook，对应五个“内置链”：

- **PREROUTING**：数据包刚到达网卡，还未进行路由决策。
- **INPUT**：数据包目的地是本机。
- **FORWARD**：数据包只是路过本机，转发到其他地方。
- **OUTPUT**：本机产生的数据包向外发送。
- **POSTROUTING**：数据包即将离开网卡。

### 四表

表决定了对数据包进行什么类型的操作。主要的表有四个：

1. **Filter**：负责放行或拦截，这是最常用的表。
2. **NAT**：负责地址转换。
3. **Mangle**：负责修改数据包，如修改 TTL、打标记。
4. **Raw**：负责配置豁免，用于跳过连接跟踪。

### 链与表的关系

并不是每一个“表”都存在于每一个“链”中。它们的关系就像是一张矩阵图，每个交叉点代表一个具体的处理时机。

| 链 (Chains) \ 表 (Tables) | Raw  | Mangle | Filter | NAT  |
| ------------------------- | ---- | ------ | ------ | ---- |
| PREROUTING                | ✅    | ✅      |        | ✅    |
| INPUT                     |      | ✅      | ✅      |      |
| FORWARD                   |      | ✅      | ✅      |      |
| OUTPUT                    | ✅    | ✅      | ✅      | ✅    |
| POSTROUTING               |      | ✅      |        | ✅    |

当一个数据包经过你的机器时，它会按以下顺序穿越这些表和链：

1. 入站数据包（目的地是本机）

   ```
   PREROUTING (Raw -> Mangle -> NAT) -> [路由决策] -> INPUT (Mangle -> Filter) -> [进入进程]
   ```

2. 转发数据包（路过本机）

   ```
   PREROUTING -> [路由决策] -> FORWARD (Mangle -> Filter) -> POSTROUTING (Mangle -> NAT)
   ```

3. 出站数据包（本机产生）

   ```
   [路由决策] -> OUTPUT (Raw -> Mangle -> NAT -> Filter) -> POSTROUTING
   ```

### 自定义链

除了内置链，还可以创建自定义链。注意，自定义链必须由内置链“跳转”过去才能生效。

为什么需要自定义链？`iptables` 是线性匹配的。如果 `INPUT` 链有 100 条规则，数据包必须从第 1 条匹配到第 100 条。使用自定义链后，我们可以做到类似下面的流程：

1. 如果是 TCP 80 端口，跳转到自定义链 `WEB_RULES`。
2. 在 `WEB_RULES` 内部匹配 5 条规则。
3. 如果不匹配，直接结束，不再回溯到 `INPUT` 链中复杂的 SSH 规则里。

另外，同一个自定义链可以被不同的默认链调用。例如，你定义了一套复杂的黑名单过滤规则 `DENY_LIST`，它可以同时被 `INPUT` 链和 `FORWARD` 链调用。

举一个具体的例子，假设你的服务器运行着 Nginx (80 端口)，你希望：

1. 所有访问 80 端口的流量先经过一个“黑名单”检查。
2. 然后再经过一个“频率限制”检查。
3. 最后才允许进入。

``` bash
# 创建两个新链
iptables -N CHECK_BLACKLIST
iptables -N WEB_LIMIT

# CHECK_BLACKLIST: 如果匹配到这些 IP，直接丢弃 (DROP)
# 如果没匹配到，默认会执行 RETURN 回到主链
iptables -A CHECK_BLACKLIST -s 1.2.3.4 -j DROP
iptables -A CHECK_BLACKLIST -s 5.6.7.8 -j DROP

# WEB_LIMIT: 超过速率的包会被丢弃
iptables -A WEB_LIMIT -m limit --limit 10/s -j ACCEPT
iptables -A WEB_LIMIT -j DROP

# 配置在INPUT链中调用它们
# 1. 只要是 80 端口的流量，先跳到黑名单链
# 2. 从黑名单链“活着”回来的流量，再跳到限流链
iptables -A INPUT -p tcp --dport 80 -j CHECK_BLACKLIST
iptables -A INPUT -p tcp --dport 80 -j WEB_LIMIT
```

### 再谈表

在上面自定义链的例子中，为了简化逻辑，不显式指定表（即不写 `-t` 参数），系统会默认你在操作 Filter 表。

因为实际上自定义链并不是全局通用的，它属于特定的表。

- 你在 Filter 表 中创建的自定义链，只能被 Filter 表的内置链（INPUT, FORWARD, OUTPUT）调用。
- 你在 NAT 表 中创建的自定义链，只能被 NAT 表的内置链（PREROUTING, POSTROUTING 等）调用。

为什么需要这种限制？因为不同的表处理数据包的“工具箱”不同。

- Filter 表 的工具箱里有 `ACCEPT`（放行）和 `DROP`（丢弃）。
- NAT 表 的工具箱里有 `SNAT`（改源地址）和 `DNAT`（改目的地址）。

如果你在 Filter 表里定义了一个改 IP 的规则，系统会觉得莫名其妙，因为它没这个权限。

结合链与表的概念，给出一个完整例子：假设我们要实现将外网对 80 端口的访问，转发到内网 8080 端口，并过滤掉黑名单里的源 IP，不让它们进入内网：

``` bash
# 在NAT表创建WEB_REWRITE链
iptables -t nat -N WEB_REWRITE

# 往这个链里加规则：把 80 端口重定向到 8080
iptables -t nat -A WEB_REWRITE -p tcp --dport 80 -j REDIRECT --to-ports 8080

# 在 PREROUTING 链中调用它
iptables -t nat -A PREROUTING -p tcp --dport 80 -j WEB_REWRITE

# 在Filter表中创建一个名为WEB_FILTER的自定义链
iptables -N WEB_FILTER

# 拒绝黑名单 IP
iptables -A WEB_FILTER -s 121.2.3.4 -j DROP

# 在 INPUT 链中调用它
iptables -A INPUT -p tcp --dport 8080 -j WEB_FILTER
```

## iptables 命令

```  bash
iptables \
	-t 表名 \
	<-A/I/D/R> 规则链名 [规则号] \
	<-i/o 网卡名> \
	-p 协议名 \
	<-s 源IP/源子网> \
	--sport 源端口 \
	<-d 目标IP/目标子网> \
	--dport 目标端口 \
	-j 动作
```

## 注意事项

- **即时生效但重启失效**：`iptables` 命令修改后立即生效。但如果你不使用 `iptables-save` 命令保存，服务器重启后规则会全部消失。

- **小心锁死自己**：在调整 `INPUT` 链的默认策略为 `DROP` 之前，请务必确认你已经允许了当前 SSH 端口的连接。

  **现代替代品**：如果你的业务场景不需要复杂的底层修改，使用 `ufw` (Ubuntu) 或 `firewalld` (CentOS) 会更简单直观。

## 参考

https://wangchujiang.com/linux-command/c/iptables.html

https://www.cnblogs.com/zhujingzhi/p/9706664.html
