---
date: 2025-06-09T00:34:04+08:00
title: kafka源码阅读（8）-Kafka中的NIO封装（下）
categories: [源码阅读,kafka]
tags: [kafka,mq,源码]
draft: true
---

> [!note]
>
> 基于开源 kafka 2.5 版本。
>
> 如无特殊说明，文中代码片段将删除 debug 信息、异常触发、英文注释等代码，以便观看核心代码。

我们知道 Java NIO 中的三大组件分别是 Channel、Buffer 以及 Selector，而 kafka 在网络层中对它们进行了进一步的封装，以便向上层组件提供更加直观和方便的网络 I/O 操作，具体对应的封装类如下：

> [!tip]
>
> 1. `TransportLayer`：它是一个接口，封装了底层 NIO 的 SocketChannel。
> 2. `NetworkReceive`：封装了 NIO 的 ByteBuffer 中的读 Buffer，对网络编程中的粘包、拆包经典实现。
> 3. `NetworkSend`：封装了 NIO 的 ByteBuffer 中的写 Buffer。
> 4. `KafkaChannel`：对 TransportLayer、NetworkReceive、NetworkSend 进一步封装，屏蔽了底层的实现细节，对上层更友好。
> 5. `KSelector`：封装了 NIO 的 Selector 多路复用器组件。

在[上篇](https://nos-ae.github.io/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB7-kselector/)中我们分析了前四个类，他们代表了 Java NIO 中的 Channel 和 Buffer，这篇我们继续分析 `KSelector`（类名实际上是与 NIO 同名的 `Selector`，为了进行区分我们将其称为 `KSelector`），其代表了 NIO 中的 Selector。

按照惯例先看下类的一些核心成员：

``` java
public class Selector implements Selectable, AutoCloseable {
  // 基础组件
  private final java.nio.channels.Selector nioSelector; // Selector
  private final ChannelBuilder channelBuilder; // KafkaChannel Builder
  
  // 通道管理相关
  private final Map<String, KafkaChannel> channels; // 所有注册到本Selector的通道
  private final Set<KafkaChannel> explicitlyMutedChannels; // 被显式静默的通道
  private final Map<String, KafkaChannel> closingChannels; // 正在关闭的通道
  
  // 事件和状态追踪相关
  private final List<Send> completedSends; // 已完成的发送
  private final LinkedHashMap<String, NetworkReceive> completedReceives; // 已完成的接收
  private final Set<SelectionKey> immediatelyConnectedKeys; // 立即连接成功的key
  private final List<String> connected; // 已建立连接的通道
  private final List<String> failedSends; // 发送失败的通道
  private final Map<String, ChannelState> disconnected; // 已断开连接的通道
  private boolean madeReadProgressLastPoll = true; // 上次poll之后是否在读数据方面有进展
  
  // 内存管理相关
  private final MemoryPool memoryPool; // 内存池
  private final long lowMemThreshold; // 内存不足阈值（固定为10% * 内存池大小）
  private boolean outOfMemory; // 是否内存不足
  private Set<SelectionKey> keysWithBufferedRead; // 有缓冲数据待读的通道
  
  // 参数配置相关
  private final int maxReceiveSize; // 允许接收的最大消息字节数
  private final boolean recordTimePerConnection; // 是否记录每个连接的时间
  private final int failedAuthenticationDelayMs; // 认证失败后的延迟关闭连接时间
  
  // 其它
  private final SelectorMetrics sensors; // 监控指标
  private final IdleExpiryManager idleExpiryManager; // // 空闲连接管理器
  private final LinkedHashMap<String, DelayedAuthenticationFailureClose> delayedClosingChannels; // 延迟关闭连接
}
```

对其中几个成员稍微解释下：

- `immediatelyConnectedKeys`：SocketChannel 被配置为非阻塞的，一般 connect 后不会马上返回成功，只能等后续 OP_CONNECT 触发才是连接成功并进行下一步处理。但在某些情况下（特别是本地连接）connect 可能会马上返回成功，这时候不会触发 OP_CONNECT，因此需要记录这些 SocketChannel 便于后续处理。
- `madeReadProgressLastPoll`：在 poll 方法中，当 `madeReadProgressLastCall && dataInBuffers` 满足时，timeout 设置为 0，即 poll 不会阻塞等待事件的发生。综合来看，应该是因为如果上次 poll 没有进展的话，可能是因为内存不足等原因，这次 poll 很可能也没进展，因此就没必要 timeout=0 导致频繁的无效 poll；反之如果上次 poll 有进展，并且 dataInBuffers 满足的话，就让 poll 快速返回然后去处理缓冲区的数据以降低内存占用，但 dataInBuffers 不满足的话，也就没必要设置 timeout=0 急着让 poll 快速返回。
- `keysWithBufferedRead`：上一篇有提到，`SslTransportLayer` 有内部缓冲区，因此可能出现这样的情况：一次性从 socket 读取了多条消息到内部缓冲区，并且后续 socket 没有更多数据可读，即 OP_READ 不再触发，因此要记录这些通道并主动地去读取缓冲区剩余的消息。
