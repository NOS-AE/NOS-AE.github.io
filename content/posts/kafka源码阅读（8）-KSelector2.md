---
date: 2025-06-09T00:34:04+08:00
title: kafka源码阅读（8）-Kafka中的NIO封装（下）
categories: [源码阅读,kafka]
tags: [kafka,mq,源码]
draft: false
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

## 总览

在[上篇](https://nosae.top/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB7-kselector/)中我们分析了前四个类，他们代表了 Java NIO 中的 Channel 和 Buffer，这篇我们继续分析 `KSelector`（类名实际上是与 NIO 同名的 `Selector`，为了进行区分我们将其称为 `KSelector`），其代表了 NIO 中的 Selector。这个类和 KafkaChannel 一样，也是服务端和客户端共用的类：客户端通过 `connect` 方法连接服务端，服务端用 `register` 方法注册接收到的服务端 socket。

我们接下来重点还是以服务端的视角来进行讲述，最后一小节再简单过下客户端相关的代码。

## 类定义

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

对其中几个成员稍微解释下，有些太细节的东西可以直接忽略：

- `disconnected`：帮助上层应用感知连接的发送失败，以便采取相应的错误处理措施
- `immediatelyConnectedKeys`：SocketChannel 被配置为非阻塞的，一般 connect 后不会马上返回成功，只能等后续 OP_CONNECT 触发才是连接成功并进行下一步处理。但在某些情况下（特别是本地连接）connect 可能会直接返回成功，这时候不会触发 OP_CONNECT，因此需要记录这些 SocketChannel 便于后续处理。
- `madeReadProgressLastPoll`：在 poll 方法中，当 `madeReadProgressLastCall && dataInBuffers` 满足时，timeout 设置为 0，即 poll 不会阻塞等待事件的发生。综合来看，应该是因为如果上次 poll 没有进展的话，可能是因为内存不足等原因，这次 poll 很可能也没进展，因此就没必要 timeout=0 导致频繁的无效 poll；反之如果上次 poll 有进展，并且 dataInBuffers 满足的话，就让 poll 快速返回然后去处理缓冲区的数据以降低内存占用，但 dataInBuffers 不满足的话，也就没必要设置 timeout=0 急着让 poll 快速返回。
- `keysWithBufferedRead`：上一篇有提到，`SslTransportLayer` 有内部缓冲区，因此可能出现这样的情况：一次性从 socket 读取了多条消息到内部缓冲区，并且后续 socket 没有更多数据可读，即 OP_READ 不再触发，因此要记录这些通道并主动地去读取缓冲区剩余的消息。

下面来解析各个服务端相关的核心方法。

## register

`register` 在之前解析 SocketServer 的时候就有见过，Processor 会将新建立的连接注册到 KSelector 上：

``` java
public void register(String id, SocketChannel socketChannel) throws IOException {
  ensureNotRegistered(id);
  // 注册OP_READ，创建KafkaChannel
  registerChannel(id, socketChannel, SelectionKey.OP_READ);
  
  // ...
}

protected SelectionKey registerChannel(String id, SocketChannel socketChannel, int interestedOps) throws IOException {
  // SocketChannel注册到Selector
  SelectionKey key = socketChannel.register(nioSelector, interestedOps);
  // 创建KafkaChannel
  KafkaChannel channel = buildAndAttachKafkaChannel(socketChannel, id, key);
  // 保存KafkaChannel
  this.channels.put(id, channel);
  // // 更新连接的访问时间
  if (idleExpiryManager != null)
    idleExpiryManager.update(channel.id(), time.nanoseconds());
  return key;
}

// 省略try-catch
private KafkaChannel buildAndAttachKafkaChannel(SocketChannel socketChannel, String id, SelectionKey key) throws IOException {
  // 创建KafkaChannel
  KafkaChannel channel = channelBuilder.buildChannel(id, key, maxReceiveSize, memoryPool,
      new SelectorChannelMetadataRegistry());
  // KafkaChannel绑定到key上，方便后续根据key取出KafkaChannel
  key.attach(channel);
  return channel;
}
```

## send

`send` 方法在 SocketServer 的 Processor 中，当 Response 类型是 SendResponse 的时候，将会调用该方法，将待发送数据保存到 KafkaChannel 并注册 OP_WRITE 等待可写事件发生时再进行写入。

``` java
public void send(Send send) {
  String connectionId = send.destination();
  KafkaChannel channel = openOrClosingChannelOrFail(connectionId);
  if (closingChannels.containsKey(connectionId)) {
    // 如果通道已关闭，记录为一次失败的发送
    // 根据failedSends，后续会将这些通道的关闭原因设置为FAILED_SEND，交给上层采取相应的错误处理措施
    this.failedSends.add(connectionId);
  } else {
    try {
      // 将send保存到KafkaChannel，并注册OP_WRITE
      channel.setSend(send);
    } catch (Exception e) {
      // 发生异常，强行关闭通道
      channel.state(ChannelState.FAILED_SEND);
      this.failedSends.add(connectionId);
      close(channel, CloseMode.DISCARD_NO_NOTIFY);
      if (!(e instanceof CancelledKeyException)) {
        log.error("Unexpected exception during send, closing connection {} and rethrowing exception {}", connectionId, e);
        throw e;
      }
    }
  }
}
```

小细节：这里关闭时传入的 DISCARD_NO_NOTIFY 是指不把断连的通道保存到 `disconnected` 中，因为 `failedSends` 已经记录了该通道，在下一次 poll 的时候会根据 `failedSends` 将这些通道保存到 `disconnected` 中，避免重复保存。

## poll

`poll` 方法是 KSelector 最核心的方法了。它会调用 Selector 的 poll 方法等待 I/O 事件就绪。当 OP_CONNECT 就绪，会将记录相关通道；当 OP_WRITE 就绪，会调用 KafkaChannel.write 将之前保存的 send 发送到网络；当 OP_READ 就绪，会调用 KafkaChannel.read 从网络读取数据。这个方法返回后，上层应用可以进一步通过  completedSends(), completedReceives(), connected(), disconnected() 这四个方法来处理结果。

另外这个方法源码中的注释也提到了[之前](https://nosae.top/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB6-socketserver/)说的请求处理顺序，poll 保证了在每次调用后，每个通道只有一个请求保存在 `completedReceives` 待处理，保证请求的发送的顺序与处理顺序相同。

``` java
public void poll(long timeout) throws IOException {
  boolean madeReadProgressLastCall = madeReadProgressLastPoll;
  // 清除上次poll的结果
  clear();

  boolean dataInBuffers = !keysWithBufferedRead.isEmpty();

  // 如果有客户端立刻连接成功的socket，或者存在缓冲区有未读数据的通道
  // 则设置poll的timeout为0，即不阻塞等待I/O事件触发
  if (!immediatelyConnectedKeys.isEmpty() || (madeReadProgressLastCall && dataInBuffers))
    timeout = 0;

  // 将之前由于内存压力而静默的通道取消静默
  if (!memoryPool.isOutOfMemory() && outOfMemory) {
    log.trace("Broker no longer low on memory - unmuting incoming sockets");
    for (KafkaChannel channel : channels.values()) {
      if (channel.isInMutableState() && !explicitlyMutedChannels.contains(channel)) {
        channel.maybeUnmute();
      }
    }
    outOfMemory = false;
  }

  long startSelect = time.nanoseconds();
  // 这里真正select
  int numReadyKeys = select(timeout);
  long endSelect = time.nanoseconds();
  this.sensors.selectTime.record(endSelect - startSelect, time.milliseconds());

  // 检查本次poll是否有东西要处理，包括
  // 1. 就绪的I/O事件
  // 2. 建立连接直接成功的通道
  // 3. 缓冲区非空的通道
  if (numReadyKeys > 0 || !immediatelyConnectedKeys.isEmpty() || dataInBuffers) {
    // 获取就绪的I/O事件
    Set<SelectionKey> readyKeys = this.nioSelector.selectedKeys();

    // 处理缓冲区非空的通道
    if (dataInBuffers) {
      keysWithBufferedRead.removeAll(readyKeys);
      Set<SelectionKey> toPoll = keysWithBufferedRead;
      keysWithBufferedRead = new HashSet<>();
      pollSelectionKeys(toPoll, false, endSelect);
    }

    // 处理就绪的I/O事件
    pollSelectionKeys(readyKeys, false, endSelect);
    readyKeys.clear();

    // 处理直接建立成功的连接
    pollSelectionKeys(immediatelyConnectedKeys, true, endSelect);
    immediatelyConnectedKeys.clear();
  } else {
    madeReadProgressLastPoll = true; //no work is also "progress"
  }

  long endIo = time.nanoseconds();
  this.sensors.ioTime.record(endIo - endSelect, time.milliseconds());

  // 处理延迟关闭的连接
  completeDelayedChannelClose(endIo);

  // 处理超时的空闲连接
  maybeCloseOldestConnection(endSelect);
}
```

再重复提一嘴，所谓“缓冲区非空的通道”是因为启用了 SSL 的 socket 数据要加密传输，因此通道的缓冲区中可能有未完全加密/解密到的数据。而 I/O 事件准备就绪的通道则可以看成是数据在 socket 的操作系统缓冲区中。前者不会触发 I/O 事件因此需要单独处理。同样地，在 connect 时就直接建立成功的连接也不会触发 I/O 事件，因此也需要单独处理。尽管存在单独处理的通道，但实际上都是调用 `pollSelectionKeys` 来处理，这体现了将 Channel 和 Buffer 的底层读写等操作统一封装在 `KafkaChannel` 的好处。

最后，延迟关闭以及超时空闲这些并不核心的操作都放在了每次 poll 调用的最后才去处理。

## pollSelectionKeys

这个方法用来处理上述的三种就绪通道，进行真正的 socket 级别的读写操作。传入的是 SelectionKey，通过这个 key 可以拿到对应的 SocketChannel 以及 KafkaChannel。

``` java
void pollSelectionKeys(
  // 就绪的key集合，通过key可以拿到就绪的I/O事件、SocketChannel以及KafkaChannel
  Set<SelectionKey> selectionKeys,
  boolean isImmediatelyConnected,
  long currentTimeNanos
) {
  // 遍历keys时可能会打乱遍历的顺序
  for (SelectionKey key : determineHandlingOrder(selectionKeys)) {
    KafkaChannel channel = channel(key);
    long channelStartTimeNanos = recordTimePerConnection ? time.nanoseconds() : 0;
    boolean sendFailed = false;
    String nodeId = channel.id();

    sensors.maybeRegisterConnectionMetrics(nodeId);
    // 更新连接的访问时间
    if (idleExpiryManager != null)
        idleExpiryManager.update(nodeId, currentTimeNanos);

    try {
      // 处理OP_CONNECT（只是将连接记录到connected）
      if (isImmediatelyConnected || key.isConnectable()) {
        if (channel.finishConnect()) {
          this.connected.add(nodeId);
          this.sensors.connectionCreated.record();

          SocketChannel socketChannel = (SocketChannel) key.channel();
          log.debug("Created socket with SO_RCVBUF = {}, SO_SNDBUF = {}, SO_TIMEOUT = {} to node {}",
            socketChannel.socket().getReceiveBufferSize(),
            socketChannel.socket().getSendBufferSize(),
            socketChannel.socket().getSoTimeout(),
            nodeId);
        } else {
          continue;
        }
      }

      // 处理已建立但未ready的连接（即未握手和认证）
      if (channel.isConnected() && !channel.ready()) {
        // 进行握手和认证
        channel.prepare();
        // 记录一些监控指标
        if (channel.ready()) {
          long readyTimeMs = time.milliseconds();
          boolean isReauthentication = channel.successfulAuthentications() > 1;
          if (isReauthentication) {
            sensors.successfulReauthentication.record(1.0, readyTimeMs);
            if (channel.reauthenticationLatencyMs() == null)
              log.warn(
                  "Should never happen: re-authentication latency for a re-authenticated channel was null; continuing...");
            else
              sensors.reauthenticationLatency
                  .record(channel.reauthenticationLatencyMs().doubleValue(), readyTimeMs);
          } else {
            sensors.successfulAuthentication.record(1.0, readyTimeMs);
            if (!channel.connectedClientSupportsReauthentication())
              sensors.successfulAuthenticationNoReauth.record(1.0, readyTimeMs);
          }
          log.debug("Successfully {}authenticated with {}", isReauthentication ?
              "re-" : "", channel.socketDescription());
        }
      }
      
      if (channel.ready() && channel.state() == ChannelState.NOT_CONNECTED)
        channel.state(ChannelState.READY);
      // 对于需要认证的连接，一些客户端请求可能在认证完成后发送，但是在响应发送前认证失效并且需要重新认证
      // 在重新认证的过程中可能会有一些服务端响应到达，会先将这些响应缓存起来
      // 再用这个方法取出，添加到complectedReceives
      Optional<NetworkReceive> responseReceivedDuringReauthentication = channel.pollResponseReceivedDuringReauthentication();
      responseReceivedDuringReauthentication.ifPresent(receive -> {
        long currentTimeMs = time.milliseconds();
        addToCompletedReceives(channel, receive, currentTimeMs);
      });

      // 处理OP_READ，从socket读取数据，或者从内部缓冲区读取数据
      if (channel.ready() && (key.isReadable() || channel.hasBytesBuffered()) && !hasCompletedReceive(channel) && !explicitlyMutedChannels.contains(channel)) {
        attemptRead(channel);
      }

      if (channel.hasBytesBuffered()) {
        // 将内部缓冲区还有数据的通道加入keysWithBufferedRead，下次poll时处理
        keysWithBufferedRead.add(key);
      }

      long nowNanos = channelStartTimeNanos != 0 ? channelStartTimeNanos : currentTimeNanos;
      // 处理OP_WRITE
      try {
        attemptWrite(key, channel, nowNanos);
      } catch (Exception e) {
        sendFailed = true;
        throw e;
      }

      if (!key.isValid())
        close(channel, CloseMode.GRACEFUL);

    } catch (Exception e) {
      // 发生异常，打日志、监控指标，最后关闭channel
      String desc = channel.socketDescription();
      if (e instanceof IOException) {
        log.debug("Connection with {} disconnected", desc, e);
      } else if (e instanceof AuthenticationException) {
        boolean isReauthentication = channel.successfulAuthentications() > 0;
        if (isReauthentication)
          sensors.failedReauthentication.record();
        else
          sensors.failedAuthentication.record();
        String exceptionMessage = e.getMessage();
        if (e instanceof DelayedResponseAuthenticationException)
          exceptionMessage = e.getCause().getMessage();
        log.info("Failed {}authentication with {} ({})", isReauthentication ? "re-" : "",
          desc, exceptionMessage);
      } else {
        log.warn("Unexpected error from {}; closing connection", desc, e);
      }

      if (e instanceof DelayedResponseAuthenticationException)
        // 延迟关闭
        maybeDelayCloseOnAuthenticationFailure(channel);
      else
        // 直接关闭/优雅关闭
        close(channel, sendFailed ? CloseMode.NOTIFY_ONLY : CloseMode.GRACEFUL);
    } finally {
      maybeRecordTimePerConnection(channel, channelStartTimeNanos);
    }
	}
}
```

有了前面细节知识的铺垫，比如握手/认证、内部缓冲区等，这块代码虽然看着很长但是并不难理解。需要注意的是，传进来的 key 可能每次都是一样顺序，为了保证在内存池内存较低时后面的 key 在读操作方面不被饿死（因为从网络读数据是需要从内存池分配内存的），将会打乱这些 key 的遍历顺序。

## attemptWrite / attemptRead

讲解完这套核心的 poll 流程后，我们再来补充下 poll 中调用的读写方法，把我们上一篇的知识串了起来。首先是写操作：

``` java
private void attemptWrite(SelectionKey key, KafkaChannel channel, long nowNanos) throws IOException {
  if (channel.hasSend()
          && channel.ready()
          && key.isWritable()
          && !channel.maybeBeginClientReauthentication(() -> nowNanos)) {
    write(channel);
  }
}

void write(KafkaChannel channel) throws IOException {
  String nodeId = channel.id();
  // 写操作
  long bytesSent = channel.write();
  Send send = channel.maybeCompleteSend();
  // 这里的判断条件要注意，由于加密通道有内部缓冲区，因此bytesSent只是说从send发了多少数据到内部缓冲区中，并不是真正发送到socket的数据量
  if (bytesSent > 0 || send != null) {
    long currentTimeMs = time.milliseconds();
    if (bytesSent > 0)
      this.sensors.recordBytesSent(nodeId, bytesSent, currentTimeMs);
    // 因此send中的所有数据真正发到了socket只能由channel.maybeCompleteSend()来判断
    if (send != null) {
      // 已加入完成发送列表
      this.completedSends.add(send);
      this.sensors.recordCompletedSend(nodeId, send.size(), currentTimeMs);
    }
  }
}
```

前面也数次提到过，注意一下加密通道的内部缓冲区就行。

然后是读操作：

``` java
private void attemptRead(KafkaChannel channel) throws IOException {
  String nodeId = channel.id();
	// 读操作
  long bytesReceived = channel.read();
  if (bytesReceived != 0) {
    long currentTimeMs = time.milliseconds();
    sensors.recordBytesReceived(nodeId, bytesReceived, currentTimeMs);
    madeReadProgressLastPoll = true;

    NetworkReceive receive = channel.maybeCompleteReceive();
    if (receive != null) {
      // 加入已完成接收列表
      addToCompletedReceives(channel, receive, currentTimeMs);
    }
  }
  if (channel.isMuted()) {
    // 这里channel由于内存池内存不足而主动静默
    outOfMemory = true;
  } else {
    madeReadProgressLastPoll = true;
  }
}
```

## 补充

上面是以服务端的视角去讲解的 KSelector 中几个核心方法，即多路复用以及具体的网络读写。本小节再来补充一下其它次重要的方法。

### connect

显而易见 `connect` 用于客户端向服务端发起网络连接。然后服务端这边就是之前分析过的 Acceptor 检测到后会交给 Processor，随后注册到 KSelector 上...

``` java
public void connect(String id, InetSocketAddress address, int sendBufferSize, int receiveBufferSize) throws IOException {
  ensureNotRegistered(id);
  // 创建SocketChannel
  SocketChannel socketChannel = SocketChannel.open();
  SelectionKey key = null;
  try {
    // 配置SocketChannel
    configureSocketChannel(socketChannel, sendBufferSize, receiveBufferSize);
    // 发起对服务端的连接
    boolean connected = doConnect(socketChannel, address);
    // 创建KafkaChannel
    key = registerChannel(id, socketChannel, SelectionKey.OP_CONNECT);

    if (connected) {
      // 直接建立成功的连接不会触发OP_CONNECT，需要主动保存下来后续处理
      log.debug("Immediately connected to node {}", id);
      immediatelyConnectedKeys.add(key);
      key.interestOps(0);
    }
  } catch (IOException | RuntimeException e) {
    if (key != null)
      immediatelyConnectedKeys.remove(key);
    channels.remove(id);
    socketChannel.close();
    throw e;
  }
}

private void configureSocketChannel(SocketChannel socketChannel, int sendBufferSize, int receiveBufferSize) throws IOException {
  // 设置SocketChannel为非阻塞（NIO）
  socketChannel.configureBlocking(false);
  Socket socket = socketChannel.socket();
  // TCP Keepalive
  socket.setKeepAlive(true);
  // TCP发送缓冲区的大小（outbound）
  if (sendBufferSize != Selectable.USE_DEFAULT_BUFFER_SIZE)
    socket.setSendBufferSize(sendBufferSize);
  // TCP接收缓冲区的大小（inbound）
  if (receiveBufferSize != Selectable.USE_DEFAULT_BUFFER_SIZE)
    socket.setReceiveBufferSize(receiveBufferSize);
  // 禁用TCP Nagle算法，提高数据传输实时性
  socket.setTcpNoDelay(true);
}
```

### close

KSelector 有好几个 close 方法，这里说的是以特定模式关闭通道的 close 方法，其它 close 方法最终都会调用这个 close：

``` java
private void close(KafkaChannel channel, CloseMode closeMode) {
  // 底层会注销key，相当于selector不会再触发该key的任何事件
  channel.disconnect();
  
  connected.remove(channel.id());

  if (closeMode == CloseMode.GRACEFUL && maybeReadFromClosingChannel(channel)) {
    // 优雅关闭连接
    closingChannels.put(channel.id(), channel);
    log.debug("Tracking closing connection {} to process outstanding requests", channel.id());
  } else {
    // 强行关闭连接
    doClose(channel, closeMode.notifyDisconnect);
  }
  this.channels.remove(channel.id());

  if (delayedClosingChannels != null)
    delayedClosingChannels.remove(channel.id());

  if (idleExpiryManager != null)
    idleExpiryManager.remove(channel.id());
}

// 从优雅关闭的通道中读取请求，并告知上层该通道是否有待处理的请求
private boolean maybeReadFromClosingChannel(KafkaChannel channel) {
  boolean hasPending;
  if (channel.state().state() != ChannelState.State.READY)
    hasPending = false;
  else if (explicitlyMutedChannels.contains(channel) || hasCompletedReceive(channel))
    // 有待处理的请求（如果通道静默的话，可能只是因为内存不足而静默，要等其解除静默后再次检查）
    hasPending = true;
  else {
    // 没有待处理的请求，那么尝试读取
    try {
      attemptRead(channel);
      // 注意这里的hasPending的值
      hasPending = hasCompletedReceive(channel);
    } catch (Exception e) {
      log.trace("Read from closing channel failed, ignoring exception", e);
      hasPending = false;
    }
  }
  return hasPending;
}
```

所谓优雅关闭的概念之前已经说过很多次，但不同的优雅关闭又可能会有着具体的差别。这里的优雅关闭通道指的是先处理完已经接收到的请求再关闭连接，但是在这个过程中，不会再触发任何的 OP_READ（因为key已经被取消）。

另外注意到一个细节，在从优雅关闭连接中读取更多更多请求的过程中，如果读不到一个完整的请求的话，也会被视为后续没有更多的请求，从而被直接关闭连接，在网络的对方节点看来，就是数据发一半然后发现连接被关闭了。

在 close 方法中如果 maybeReadFromClosingChannel 认为通道后续还有待接收/处理的请求，将优雅关闭的通道记录到 `closingChannels`。随后在每次 poll 开头调用的 clear 方法中，除了清理上一次 poll 的结果外，还会处理这些通道，继续尝试读取更多的请求或者关闭连接：

``` java
private void clear() {
  // ...

	// 继续处理优雅关闭的连接
  for (Iterator<Map.Entry<String, KafkaChannel>> it = closingChannels.entrySet().iterator(); it.hasNext(); ) {
    KafkaChannel channel = it.next().getValue();
    boolean sendFailed = failedSends.remove(channel.id());
    boolean hasPending = false;
    if (!sendFailed)
      hasPending = maybeReadFromClosingChannel(channel);
    if (!hasPending || sendFailed) {
      doClose(channel, true);
      it.remove();
    }
  }

  // ...
}
```

## 总结

本篇续上篇介绍了在 kafka 中封装了 Java NIO Selector 的 KSelector 类，包括 socket 注册与通道创建、多路复用轮询、I/O 事件处理以及优雅关闭连接等内容，并且针对 Java NIO、TCP 加密传输的特点进行了一些特殊处理，在此就不再赘述，都在上面文中体现了。

总而言之，这个类为服务端和客户端的上层组件屏蔽了许多 NIO 麻烦的处理细节，在 ServerSocket 中我们可以看出，上层组件基本上只需要「注册、轮询、处理封装好的结果」三部曲，十分方便。

## 参考

[图解 Kafka 网络层实现机制之 Selector 多路复用器](https://www.51cto.com/article/713648.html)
