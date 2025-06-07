---
title: kafka源码阅读（7）-KSelector与KafkaChannel
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

这些类在[上一篇](https://nos-ae.github.io/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB6-socketserver/)对 `Acceptor` 和 `Processor` 中多少有接触过，但为了避免过于深入细节而失去对 `Acceptor` 和 `Processor` 全局的把握，因此并没有深入分析这些类。本篇自底向上地去分析对这些类展开深入分析，即先分析被依赖的类。

## TransportLayer

`TransportLayer` 接口用于通信，底层封装了 `SocketChannel` 。这个接口继承了 Java NIO 中的 `ScatteringByteChannel` 和 `GatheringByteChannel` 这两个接口，这说明 `TransportLayer` 除了提供最基本的读写能力外，还支持高级 I/O 中的散布读（scatter read）以及聚集写（gather write）能力，仅通过一次（系统）调用就能读写多个非连续缓冲区，在处理网络协议时，就可以将 header 和 body 保存在不同缓冲区中集中地进行散布读和聚集写，提高效率。

`TransportLayer` 有两个实现类，分别是用于明文传输的 `PlaintextTransportLayer` 以及用于 SSL 加密传输的 `SslTransportLayer`。由于本篇的目的不是身份认证，因此只分析明文传输实现类。看下类定义：

``` java
public class PlaintextTransportLayer implements TransportLayer {
  // NIO中的事件key
  private final SelectionKey key;
  // key对应的socket
  private final SocketChannel socketChannel;
  // 客户端身份，由于明文传输不需身份认证，因此是匿名身份
  private final Principal principal = KafkaPrincipal.ANONYMOUS;
}
```

这里 `key` 成员是在 `SocketServer` 的 `configureNewConnections` 方法调用的时候传入的，调用路径是：

```
SocketServer.configureNewConnections -> KSelector.register -> KSelector.registerChannel（注册SocketChannel到Selector上得到key） -> KSelector.buildAndAttachKafkaChannel（创建KafkaChannel，并将其作为key作为attachment，便于后续根据key反向获取KafkaChannel的引用）
```

`KafkaChannel` 的方法基本上就只是简单调用了 `SocketChannel` 的同名方法，比如：

``` java
public int read(ByteBuffer dst) throws IOException {
  return socketChannel.read(dst);
}

public long write(ByteBuffer[] srcs) throws IOException {
  return socketChannel.write(srcs);
}
```

需要注意的只有这个 `finishConnect` 方法，由于 `SocketChannel` 被设置成非阻塞的，因此外界需要通过 `finishConnect` 方法检查连接是否已经成功，连接成功后，注册 `OP_READ` 事件等待从 socket 读取对端发来的数据：

``` java
public boolean finishConnect() throws IOException {
  boolean connected = socketChannel.finishConnect();
  if (connected)
    key.interestOps(key.interestOps() & ~SelectionKey.OP_CONNECT | SelectionKey.OP_READ);
  return connected;
}
```

相比之下，`SslTransportLayer` 的实现就复杂许多，因为涉及到数据加密操作，不再是简单地调用 `SocketChannel` 同名方法。比如在 `transferFrom` 方法的实现中，`PlainTransportLayer` 只是调用了 `SocketChannel.transferTo`，而 `SslTransportLayer` 还需要申请堆外内存缓冲来存放文件的数据，加密之后再写入对端网络节点，感兴趣可以的读者自行查看源码。

另外还有个 `hasPendingWrites` 方法，在 `PlaintextTransportLayer` 中固定返回 false，而 `SslTranportLayer` 中返回 `netWriteBuffer.hasRemaining()`。这同样是因为加密的原因，调用 `SslTransportLayer` 的 `write()` 方法时，数据首先会加密并保存在 `netWriteBuffer` 中，然后再写入 `SocketChannel`，因此 `SslTransportLayer` 相当于多了个内部缓冲 `netWriteBuffer`， `hasPendingWrites` 就是用于检查内部缓冲的数据是否已经全部写入 `SocketChannel`。

## NetworkReceive

`NetworkReceive` 封装了 NIO 中的读 Buffer，核心是解决 TCP 的粘包拆包问题。我们在应用层协议上大多以“包”为单位进行传输，而面向字节流的 TCP 并不知道“包”的存在。数据通过 TCP 发送时，如果数据量没达到 TCP 缓冲区大小，TCP 可能会将多个请求合并成同一个请求进行发送，这就形成了所谓的**粘包问题**。而如果一个包过大的话，TCP 又会将其拆分为多次发送，形成**拆包问题**。说白了其实就是 TCP 是面向字节流的，每次发送或者接收的到的 TCP 报文并不与应用层的包一一对应，因此我们需要在应用层从字节流中识别并组装成一个完整的包，而 TCP 的字节流承诺有序和可靠性，因此应用层只需聚焦于如何识别出一个包的边界。

常见的定义边界方法有固定包大小、特殊分隔符、消息头定义包大小。kafka 使用的是第三种，即包由 4 个字节的消息体大小以及消息体组成，如图所示：

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20250602235512339.png" alt="image-20250602235512339" style="zoom:50%;" />

了解完背景知识后，回到我们的代码中，先看一下 `NetworkReceive` 类的定义：

``` java
public class NetworkReceive implements Receive {
  // 连接id，即下一小节中的KafkaChannel.id
  private final String source;
  // 用于接收消息体大小的缓冲区，固定4字节
  private final ByteBuffer size;
  // 消息最大大小
  private final int maxSize;
  // 内存池
  private final MemoryPool memoryPool;
  // 记录从size缓冲区中解析出的消息体大小
  private int requestedBufferSize = -1;
  // 用于接收消息体的缓冲区
  private ByteBuffer buffer;
}
```

这个类的成员比较简单，不再解释。核心方法只有 `readFrom`，从 `Channel` 中读取一条消息并存到 `Buffer` 中，我们之前说的消息边界识别就是在这里进行的：

``` java
public long readFrom(ScatteringByteChannel channel) throws IOException {
  int read = 0;
  if (size.hasRemaining()) {
    // 读取size
    int bytesRead = channel.read(size);
    if (bytesRead < 0)
      throw new EOFException();
    read += bytesRead;
    if (!size.hasRemaining()) {
      // size读取完毕，将其转换成整数
      size.rewind();
      int receiveSize = size.getInt();
      if (receiveSize < 0)
        throw new InvalidReceiveException("Invalid receive (size = " + receiveSize + ")");
      if (maxSize != UNLIMITED && receiveSize > maxSize)
        throw new InvalidReceiveException("Invalid receive (size = " + receiveSize + " larger than " + maxSize + ")");
      requestedBufferSize = receiveSize; //may be 0 for some payloads (SASL)
      if (receiveSize == 0) {
        buffer = EMPTY_BUFFER;
      }
    }
  }
  // 根据size分配消息体的缓冲区
  if (buffer == null && requestedBufferSize != -1) {
    buffer = memoryPool.tryAllocate(requestedBufferSize);
    if (buffer == null)
      log.trace("Broker low on memory - could not allocate buffer of size {} for source {}", requestedBufferSize, source);
  }
  // 从Channel读入消息体
  if (buffer != null) {
    int bytesRead = channel.read(buffer);
    if (bytesRead < 0)
      throw new EOFException();
    read += bytesRead;
  }

  return read;
}
```

需要注意的是，由于拆包问题，可能需要多次 `readFrom` 的调用才能读完整个包。最后外界通过 `complete` 方法轮询是否已经完成接收，`payload` 方法获取接收的消息缓冲：

``` java
public boolean complete() {
  return !size.hasRemaining() && buffer != null && !buffer.hasRemaining();
}

public ByteBuffer payload() {
  return this.buffer;
}
```

## NetworkSend

`NetworkSend` 也很简单，这个类继承自 `ByteBufferSend`，实现了 `Send` 接口。主要就是通过 `writeTo` 方法将 `Buffer` 中的数据写入 `Channel`，并通过 `completed` 方法检查是否已经全部写入完毕，这两个方法都是 `ByteBufferSend` 中的。

``` java
public class ByteBufferSend implements Send {
  public long writeTo(GatheringByteChannel channel) throws IOException {
    long written = channel.write(buffers);
    if (written < 0)
      throw new EOFException("Wrote negative bytes to channel. This shouldn't happen.");
    remaining -= written;
    // 只有SslTransportLayer（加密传输）才可能pending
    pending = TransportLayers.hasPendingWrites(channel);
    return written;
  }

  public boolean completed() {
    return remaining <= 0 && !pending;
  }
}
```

可以看到 `ByteBufferSend` 包装的是若干个 `Buffer`。`NetworkSend` 只需要两个 `Buffer`，分别对应之前说的 size 以及消息体：

``` java
public class NetworkSend extends ByteBufferSend {
  public NetworkSend(String destination, ByteBuffer buffer) {
    super(destination, sizeBuffer(buffer.remaining()), buffer);
  }

  private static ByteBuffer sizeBuffer(int size) {
    ByteBuffer sizeBuffer = ByteBuffer.allocate(4);
    sizeBuffer.putInt(size);
    sizeBuffer.rewind();
    return sizeBuffer;
  }
}
```

## KafkaChannel

KafkaChannel 用于表示一条客户端与服务端之间的连接，提供了底层 I/O 读写、身份认证、通道状态管理等功能。KafkaChannel 既服务于客户端代码也服务于服务端代码，即如图所示：

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20250602103143269.png" alt="image-20250602103143269" style="zoom:50%;" />

先看一下类定义：

``` java
public class KafkaChannel implements AutoCloseable {
  // 唯一标识整个系统中的网络连接，如localAddr:localPort-remoteAddr:remotePort-index
  private final String id;
  
  // 数据传输相关
  private final TransportLayer transportLayer; // 传输层对象，底层使用SocketChannel进行读写
  private SocketAddress remoteAddress; // 客户端网络地址
  private NetworkReceive receive; // 当前正在处理的接收操作，包含从客户端接收的数据
  private Send send; // 当前正在处理的发送操作，包含要发送给客户端的数据
  private final int maxReceiveSize; // 允许接收的最大消息字节数
  private long networkThreadTimeNanos; // 指标监控，网络线程处理该通道的累计时间
  
  // 认证相关，处理SASL等认证机制
  private final Supplier<Authenticator> authenticatorCreator;
  private Authenticator authenticator; // Authenticator底层使用TransportLayer进行读写，
  private int successfulAuthentications;
  private long lastReauthenticationStartNanos;
  
  // 状态相关
  private ChannelState state; // 通道当前状态，如认证中、关闭中等状态
  private boolean disconnected; // 通道是否已断开连接
  private ChannelMuteState muteState; // 静音状态，控制是否接收新请求，以及连接断开后是否有待处理的请求
  private boolean midWrite; // 是否正在进行写操作，防止并发写入导致的数据损坏
 
  // 资源管理相关
  private final MemoryPool memoryPool; // 内存池，用于复用内存，避免频繁分配和释放
  private final ChannelMetadataRegistry metadataRegistry; // 记录通道相关的元数据，例如KIP-511引入的客户端软件名称、版本等
}
```

数据传输相关的成员中，之前介绍了核心的 `TransportLayer`、`NetworkReceive` 以及 `NetworkSend`，可以将他们三个分别对应于 NIO 中的 `Channel` 以及读写 `Buffer`，但是在实际进行读写的时候，NIO 的做法是调用 `Channel` 的读写方法并传入 `Buffer`，而 kafka 这里是调用 `NetworkReceive` / `NetworkSend` 的读写方法并传入 `TransportLayer`，这里注意一下即可。

下面我们从 `KafkaChannel` 生命周期中被调用的方法逐个讲解分析。但因为 `KafkaChannel` 与最后一节要讲的 `KSelector` 息息相关，因此期间可能会涉及到 `KSelector`，读者可以对比观看甚至自行查阅源码，以便心中有数。

首先是用于检查连接是否已建立的 `finishConnect` 方法：

``` java
public boolean finishConnect() throws IOException {
  SocketChannel socketChannel = transportLayer.socketChannel();
  if (socketChannel != null) {
    remoteAddress = socketChannel.getRemoteAddress();
  }
  boolean connected = transportLayer.finishConnect();
  if (connected) {
    if (ready()) {
      state = ChannelState.READY;
    } else if (remoteAddress != null) {
      state = new ChannelState(ChannelState.State.AUTHENTICATE, remoteAddress.toString());
    } else {
      state = ChannelState.AUTHENTICATE;
    }
  }
  return connected;
}
```

TCP 连接建立完成后，接下来是用于 SSL 握手和 SASL 认证操作的 `prepare` 方法：

``` java
public void prepare() throws AuthenticationException, IOException {
  boolean authenticating = false;
  try {
    if (!transportLayer.ready())
      transportLayer.handshake();
    if (transportLayer.ready() && !authenticator.complete()) {
      authenticating = true;
      authenticator.authenticate();
    }
  } catch (AuthenticationException e) {
    // 握手或认证失败，state更新为AUTHENTICATION_FAILED
    String remoteDesc = remoteAddress != null ? remoteAddress.toString() : null;
    state = new ChannelState(ChannelState.State.AUTHENTICATION_FAILED, e, remoteDesc);
    if (authenticating) {
      // 如果是认证失败，要延迟关闭连接，防止暴力破解
      // 延迟时间由参数connection.failed.authentication.delay.ms决定
      delayCloseOnAuthenticationFailure();
      throw new DelayedResponseAuthenticationException(e);
    }
    throw e;
  }
  if (ready()) {
    // 握手与认证完成后，state转到READY
    ++successfulAuthentications;
    state = ChannelState.READY;
  }
}

// 延迟关闭期间，禁止OP_WRITE
private void delayCloseOnAuthenticationFailure() {
  transportLayer.removeInterestOps(SelectionKey.OP_WRITE);
}
```

下面就是网络读写操作相关方法，首先是用于预发送的 `setSend`，这个方法将待发送数据保存到 `KafkaChannel` 的 `send` 成员，注册 `OP_WRITE` 监听写事件的发生，后续将调用 `write` 方法发送数据：

``` java
public void setSend(Send send) {
  if (this.send != null)
    throw new IllegalStateException("Attempt to begin a send operation with prior send operation still in progress, connection id is " + id);
  this.send = send;
  this.transportLayer.addInterestOps(SelectionKey.OP_WRITE);
}
```



##  参考

[图解 Kafka 网络层实现机制（一）](https://www.51cto.com/article/711962.html)

[图解 Kafka 网络层实现机制之 Selector 多路复用器](https://www.51cto.com/article/713648.html)