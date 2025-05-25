---
title: kafka源码阅读（6）-SocketServer
categories: [随笔]
tags: [kafka,mq,源码]
---

> [!note]
>
> 基于开源 kafka 2.5 版本。
>
> 如无特殊说明，文中代码片段将删除 debug 信息、异常触发、英文注释等代码，以便观看核心代码。

本章开始上点干货，真正看一下 kafka 的网络层是如何运作的，主要是解析 `SocketServer` 这个核心类。

## broker 服务启动

首先我们从 broker 服务启动开始看起。从 Kafka.scala 的 `main` 函数开始，会调用到 `KafkaServer.startup` 函数，这个函数负责启动一堆的组件（比如 zk 客户端、log、group coordinator），其中我们关注到 `socketServer` 这个组件的构造与启动：

``` scala
socketServer = new SocketServer(config, metrics, time, credentialProvider)
socketServer.startup(startupProcessors = false)
```

SocketServer 是一个套接字服务，

## 参考

[Kafka源码阅读01: 网络层阅读之服务器的启动 - BewareMyPower的博客](https://bewaremypower.github.io/2019/09/18/Kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB01-%E7%BD%91%E7%BB%9C%E5%B1%82%E9%98%85%E8%AF%BB%E4%B9%8B%E6%9C%8D%E5%8A%A1%E5%99%A8%E7%9A%84%E5%90%AF%E5%8A%A8/)

极客时间《Kafka核心源码解读》——胡夕