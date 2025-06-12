---
date: 2025-06-11T18:42:13+08:00
title: Kafka源码阅读（9）-SocketServer之DataPlane与ControlPlane
tags: [kafka,mq,源码]
categories: [源码阅读,kafka]
draft: true
---

在本系列的[kafka源码阅读（6）-SocketServer 之 Processor 与 Acceptor](https://nosae.top/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB6-socketserver/)中，主要介绍了负责实现网络编程 reactor 模式的 Acceptor 以及 Processor，对于 SocketServer 如何使用它们并没有介绍到，那么这篇将真正揭开 SocketServer 的神秘面纱。

不知从何说起，先把 SocketServer 类的官方注释翻译一下：

> SocketServer 负责处理 kafka 的网络连接、请求和响应。并且将请求类型分为数据类请求以及控制类请求，支持分别用两种类型的请求平面来处理这些请求：
>
> 1. **数据平面 Data Plane**：
>    - 职责：负责处理来自客户端的数据类型请求（如果没开启控制平面，则数据平面将处理所有请求）。
>    - 线程模型：每个 listener 对应 1 个 Acceptor 线程（可以配置多个 listener），1 个 Acceptor 对应 N 个 Processor 线程，Acceptor 接收到的每条连接交由其中 1 个 Processor 进行处理。另外还有 M 个 Handler 线程负责处理连接上发来的请求，得到响应后交回给 Processor 去返回给客户端，注意这 M 个 Handler 并没有与其它线程、连接或者请求有什么对应关系，是整个数据平面中公共的处理请求的线程池。
> 2. **控制平面 Control Plane**：
>    - 职责：负责处理来自 controller 的控制类型请求，如上所述，控制平面是可选的。
>    - 线程模型：整个控制平面只有 1 个 Acceptor 线程负责接建立连接，并且该 Acceptor 只有一个 Processor 线程负责处理所有连接。另外也只有 1 个 Handler 线程来处理所有请求。相比数据平面的线程模型比较“寒酸”，因为控制类型请求的数量远小于数据类型的请求。

从注释的开始我们就看出 SocketServer 基本负责整个 kafka 的网络请求处理，并且出于[一些原因（KAFKA-4453）](https://issues.apache.org/jira/browse/KAFKA-4453)，SocketServer 将客户端请求分为了数据和控制类型，并且分别用数据平面和控制平面来处理这些请求，以保证控制类请求得到优先处理（不禁想起文件传输协议 FTP 也将连接分成了控制连接和数据连接）。

listener 其实就是 endpoint。endpoint 由主机、端口以及安全协议组成：

``` scala
case class EndPoint(
  host: String, 
  port: Int,
  listenerName: ListenerName, 
  securityProtocol: SecurityProtocol
)
```

具体的用户配置举例如下，总共 3 个 listener：

```shell
listener.security.protocol.map=CONTROLLER:PLAINTEXT,INTERNAL:PLAINTEXT,EXTERNAL:SSL
listeners=CONTROLLER://192.1.1.8:9091,INTERNAL://192.1.1.8:9092,EXTERNAL://10.1.1.5:9093
```

最后，我们再捋一下数据平面的线程模型，一图胜千言，直接上图：

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20250613015817144.png" alt="image-20250613015817144" style="zoom:50%;" />

这个图可以和剖析 Processor 和 Acceptor 那篇中的图对比着观看。

