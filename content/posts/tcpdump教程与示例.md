---
date: 2026-02-20T17:21:02+08:00
title: tcpdump教程与示例
tags: [网络,tcp,抓包,tcpdump]
categories: [网络]
repost: https://danielmiessler.com/blog/tcpdump
draft: false
---

![tcpip header](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/ip-header-2021-1024x505.png)

tcpdump 是全球领先的网络分析工具——将强大功能与简洁性结合于一个命令行界面中。本指南将向您展示如何使用它。

tcpdump 是一款功能强大的命令行数据包分析工具。它允许您实时捕获并检查网络流量。对于网络管理员、安全专业人员以及任何需要理解网络行为的人来说，这个工具都是无价的。

在本教程中，我们将探讨 50 个使用 tcpdump 的实用示例。这些示例将涵盖广泛的使用场景，从基础流量捕获到高级过滤与分析。

## 基本语法

tcpdump 的基本语法如下：

``` bash
tcpdump [options] [expression]
```

- `options`：修改 tcpdump 的行为，例如指定要捕获的接口或输出格式。
- `expression`：定义要捕获的流量类型。在这里您可以指定主机名、IP 地址、端口、协议以及其他条件。

## 在接口上捕获流量

要捕获某个特定接口上的所有流量，请使用 `-i` 标志并在其后指定接口名称。例如，要捕获 eth0 接口上的流量：

``` bash
tcpdump -i eth0
```

要查看所有可用接口的列表，请使用以下命令：

``` bash
tcpdump -D
```

## 捕获发往/来自特定主机的流量

要捕获发往或来自某个特定主机的流量，请使用 host 关键字并在其后指定主机名或 IP 地址：

``` bash
tcpdump host 192.168.1.100
```

这将捕获与 IP 地址为 192.168.1.100 的主机之间的所有流量。

## 在特定端口上捕获流量

要捕获某个特定端口上的流量，请使用 port 关键字并在其后指定端口号：

``` bash
tcpdump port 80
```

这将捕获端口 80（HTTP）上的所有流量。

## 组合过滤条件

您可以使用 and、or 和 not 运算符来组合过滤条件。例如，要捕获与主机 192.168.1.100 在端口 80 上的所有往返流量，请使用：

``` bash
tcpdump host 192.168.1.100 and port 80
```

要捕获来自 192.168.1.100 且端口为 80 或 443 的流量，请使用：

``` bash
tcpdump src host 192.168.1.100 and \( port 80 or port 443 \)
```

## 高级过滤

### 按协议过滤

要按协议过滤，请使用 ip、tcp、udp 或其他协议关键字。

例如，要仅捕获 TCP 流量：

``` bash
tcpdump tcp
```

要仅捕获 UDP 流量：

``` bash
tcpdump udp
```

### 按源或目的过滤

要按源或目标主机或端口进行过滤，请使用 src 或 dst 关键字：

``` bash
tcpdump src host 192.168.1.100
```

这将捕获来自主机 192.168.1.100 的所有流量。

``` bash
tcpdump dst port 443
```

这将捕获所有发往端口 443 的流量。

### 按网络过滤

要捕获特定网络内的流量，请使用 net 关键字：

``` bash
tcpdump net 192.168.1.0/24
```

这将捕获 192.168.1.0/24 网络内的所有流量。

## 将捕获的流量保存到文件

要将捕获的流量保存到文件，请使用 -w 标志并在其后指定文件名：

``` bash
tcpdump -w capture.pcap -i eth0
```

这将把在 eth0 接口上捕获的所有流量保存到文件 capture.pcap。

您之后可以使用 tcpdump 或其他数据包分析器（例如 Wireshark）来分析此文件。

## 从文件读取捕获的流量

要从文件读取捕获的流量，请使用 -r 标志并在其后指定文件名：

``` bash
tcpdump -r capture.pcap
```

这将读取并显示文件 capture.pcap 中的流量。

## 输出详细程度

您可以使用 -v、-vv 或 -vvv 标志来控制 tcpdump 输出的详细程度。

- -v：详细输出。
- -vv：更详细的输出。
- -vvv：最详细的输出。

例如：

``` bash
tcpdump -vv -i eth0
```

## 50 个 tcpdump 示例

以下是 50 个 tcpdump 示例，帮助您在各种情况下隔离流量：

好的，明白了。这是去掉了文字加粗、原样输出并组织成 Markdown 的 45 个例子：

1. 捕获接口 eth0 上的所有流量：

   ```bash
   tcpdump -i eth0
   ```

2. 捕获接口 wlan0 上的所有流量：

   ```bash
   tcpdump -i wlan0
   ```

3. 捕获发往或来自主机 192.168.1.100 的流量：

   ```bash
   tcpdump host 192.168.1.100
   ```

4. 捕获发往或来自主机 example.com 的流量：

   ```bash
   tcpdump host example.com
   ```

5. 捕获端口 80（HTTP）上的流量：

   ```bash
   tcpdump port 80
   ```

6. 捕获端口 443（HTTPS）上的流量：

   ```bash
   tcpdump port 443
   ```

7. 捕获端口 22（SSH）上的流量：
   ```bash
   tcpdump port 22
   ```

8. 捕获端口 21（FTP）上的流量：
   ```bash
   tcpdump port 21
   ```

9. 捕获端口 25（SMTP）上的流量：

   ```bash
   tcpdump port 25
   ```

10. 捕获端口 53（DNS）上的流量：

    ```bash
    tcpdump port 53
    ```

11. 捕获来自主机 192.168.1.100 的流量：

    ```bash
    tcpdump src host 192.168.1.100
    ```
    
12. 捕获发往主机 192.168.1.100 的流量：

    ```bash
    tcpdump dst host 192.168.1.100
    ```
    
13. 捕获来自端口 80 的流量：

    ```bash
    tcpdump src port 80
    ```
    
14. 捕获发往端口 443 的流量：

    ```bash
    tcpdump dst port 443
    ```
    
15. 捕获所有 TCP 流量：

    ```bash
    tcpdump tcp
    ```
    
16. 捕获所有 UDP 流量：

    ```bash
    tcpdump udp
    ```
    
17. 捕获所有 ICMP 流量：

    ```bash
    tcpdump icmp
    ```
    
18. 捕获发往或来自网络 192.168.1.0/24 的流量：

    ```bash
    tcpdump net 192.168.1.0/24
    ```
    
19. 捕获来自网络 192.168.1.0/24 的流量：

    ```bash
    tcpdump src net 192.168.1.0/24
    ```
    
20. 捕获发往网络 192.168.1.0/24 的流量：

    ```bash
    tcpdump dst net 192.168.1.0/24
    ```
    
21. 捕获发往主机 192.168.1.100 且端口为 80 的流量：

    ```bash
    tcpdump dst host 192.168.1.100 and dst port 80
    ```
    
22. 捕获来自主机 192.168.1.100 且端口为 443 的流量：

    ```bash
    tcpdump src host 192.168.1.100 and src port 443
    ```
    
23. 捕获发往或来自主机 192.168.1.100 且端口为 80 或 443 的流量：

    ```bash
    tcpdump host 192.168.1.100 and port 80 or port 443
    ```
    
24. 捕获除 ICMP 之外的所有流量：

    ```bash
    tcpdump not icmp
    ```
    
25. 捕获除端口 80 之外的所有流量：

    ```bash
    tcpdump not port 80
    ```
    
26. 捕获具有特定 TCP 标志（SYN）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] & tcp-syn != 0'
    ```
    
27. 捕获具有特定 TCP 标志（ACK）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] & tcp-ack != 0'
    ```
    
28. 捕获具有特定 TCP 标志（RST）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] & tcp-rst != 0'
    ```
    
29. 捕获具有特定 TCP 标志（FIN）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] & tcp-fin != 0'
    ```
    
30. 捕获具有特定 TCP 标志（URG）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] & tcp-urg != 0'
    ```
    
31. 捕获具有特定 TCP 标志（PSH）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] & tcp-push != 0'
    ```
    
32. 捕获具有特定 TCP 标志（ALL）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] = 0x01'
    ```
    
33. 捕获具有特定 TCP 标志（NONE）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] = 0x00'
    ```
    
34. 捕获具有特定 TCP 标志（SYN/ACK）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] = 0x12'
    ```
    
35. 捕获具有特定 TCP 标志（SYN/RST）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] = 0x14'
    ```
    
36. 捕获具有特定 TCP 标志（SYN/FIN）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] = 0x11'
    ```
    
37. 捕获具有特定 TCP 标志（PSH/ACK）的流量：

    ```bash
    tcpdump 'tcp[tcpflags] = 0x18'
    ```
    
38. 捕获具有特定 IP 分片偏移量的流量：

    ```bash
    tcpdump 'ip[6:2] & 0x1fff != 0'
    ```
    
39. 捕获具有特定 IP TTL 的流量：

    ```bash
    tcpdump 'ip[8] = 128'
    ```
    
40. 捕获具有特定 IP DSCP 的流量：

    ```bash
    tcpdump 'ip[1] & 0xfc >> 2 = 46'
    ```
    
41. 捕获具有特定 IP ECN 的流量：

    ```bash
    tcpdump 'ip[1] & 0x03 = 3'
    ```
    
42. 捕获具有特定 TCP 序列号的流量：

    ```bash
    tcpdump 'tcp[4:4] = 12345678'
    ```
    
43. 捕获具有特定 TCP 确认号的流量：

    ```bash
    tcpdump 'tcp[8:4] = 87654321'
    ```
    
44. 捕获具有特定 TCP 源端口 range 的流量：

    ```bash
    tcpdump 'tcp[0:2] > 1023 and tcp[0:2] < 65536'
    ```
    
45. 捕获具有特定 TCP 目标端口 range 的流量：

    ```bash
    tcpdump 'tcp[2:2] > 1023 and tcp[2:2] < 65536'
    ```

这些示例应当为使用 tcpdump 分析网络流量提供坚实的基础。祝您抓包顺利！
