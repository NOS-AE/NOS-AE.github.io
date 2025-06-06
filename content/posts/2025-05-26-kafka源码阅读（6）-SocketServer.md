---
title: kafka源码阅读（6）-SocketServer中的Processor和Acceptor
categories: [源码阅读,kafka]
tags: [kafka,mq,源码]
---

> [!note]
>
> 基于开源 kafka 2.5 版本。
>
> 如无特殊说明，文中代码片段将删除 debug 信息、异常触发、英文注释等代码，以便观看核心代码。

[上一篇](https://nos-ae.github.io/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB5-%E8%AF%B7%E6%B1%82%E9%98%9F%E5%88%97/)简单过了一下 kafka 网络层中的请求队列，本章开始上点干货，真正看一下 kafka 的网络层是如何运作的。本篇的解析重点在于 SocketServer 中的 Acceptor 以及 Processor 线程。

## 总览

从 broker 服务启动开始看起。从 Kafka.scala 的 `main` 函数开始，会调用到 `KafkaServer.startup` 函数，这个函数负责启动一堆的组件（比如 zk 客户端、log、group coordinator），其中 `socketServer` 这个组件的构造与启动如下：

``` scala
socketServer = new SocketServer(config, metrics, time, credentialProvider)
socketServer.startup(startupProcessors = false)
```

这个 `SocketServer` 是 kafka 网络通信中重中之重的组件，实现与客户端之间的高性能通信。

> 比如说，当Broker处理速度很慢、需要优化的时候，你只有明确知道SocketServer组件的工作原理，才能制定出恰当的解决方案，并有针对性地给出对应的调优参数。

结合上一篇对 `RequestChannel` 的分析，这里可以直接给出 `SocketServer`对一个网络请求及其响应的流转过程，为了便于新手理解全貌，这里给出的是简化版，如图所示：

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20250526111546911.png" alt="image-20250526111546911" style="zoom:50%;" />

图中最左边的小人表示客户端，可以是 producer、consumer 或 broker。 `KafkaRequestHandlerPool` 是真正处理请求逻辑的线程池。具体地，它会调用 `KafkaApis.handle` 对不同 API 进行不同的处理逻辑。目前对这个组件有个印象即可不必深究，我们的重点还是在上面的 `SocketServer` 上。

图中的 Acceptor 线程用于接收 TCP 连接建立请求，是整个网络通信层的门面。Processor 线程之前说过，TCP 连接的建立、以及所有请求和响应由一个 Processor 线程全权负责接收和发送。可见，Acceptor 加上 Processor 就是网络编程中经典的 reactor 模式。下面将分析这两个类的源码。

## Acceptor

Acceptor 作为 reactor 模式中 dispatcher 的角色，负责接收新的连接请求然后分发给 `Processor` 线程去处理。先看一下类定义：

``` scala
private[kafka] class Acceptor(
  // Acceptor使用endpoint创建服务端socket，比如PLAINTEXT://127.0.0.1:9092
  val endPoint: EndPoint,
  // TCP发送缓冲区的大小（outbound）
  val sendBufferSize: Int,
  // TCP接收缓冲区的大小（inbound）
  val recvBufferSize: Int,
  // 当前broker的id
  brokerId: Int,
  // 控制最大连接数
  connectionQuotas: ConnectionQuotas,
  metricPrefix: String
) extends AbstractServerThread(connectionQuotas) with KafkaMetricsGroup {
	// NIO中的Selector，负责监听连接创建、读写事件等
  private val nioSelector = NSelector.open()
  // NIO中的ServerSocketChannel，后续会注册到nioSelector
  val serverChannel = openServerSocket(endPoint.host, endPoint.port)
  // Processor线程池
  private val processors = new ArrayBuffer[Processor]()
  private val processorsStarted = new AtomicBoolean
  private val blockedPercentMeter = newMeter(s"${metricPrefix}AcceptorBlockedPercent",
    "blocked time", TimeUnit.NANOSECONDS, Map(ListenerMetricTag -> endPoint.listenerName.value))
```

`Acceptor` 和 `Processor` 都继承自 `AbstractServerThread`，定义了一些公有方法比如 `shutdown`，这个类实际上是一个 `Runnable` 类。

`Acceptor` 成员中的的 `sendBufferSize` 和 `recvBufferSize` 用于设置 socket 的发送和接收缓冲区大小，可以分别通过 broker 端的 `socket.send.buffer.bytes` 和 `socket.receive.buffer.bytes` 参数进行配置，默认是 100KB。另外说个题外话，这里引出了一个生产环境的调优点：在生产环境中，如果客户端和 broker 的网络通信延迟比较大（比如RTT>10ms），这样一来带宽延迟乘积（Bandwidth delay product, BDP）就比较大，那么可以将他们的两个缓冲区大小调高些，以提高数据吞吐量，因为 BDP 表示了网络承载能力（想象一下网络是一根水管，带宽是每秒的水量，延迟是端到端的延迟，那么 BDP 就是已发出但还未到达对端的总水量），最大接收窗口就表示了网络承载能力内可以不经确认发出的报文。

`Acceptor` 中的 `connectionQuotas` 用于管理连接配额，简单来说包括：限制每个 IP 地址的最大连接数、整个 broker 的最大连接数、为不同监听器（listener）设置不同的连接限制，并且支持动态更新连接配额配置。

`Acceptor` 成员中的 `nioSelector` 和 `serverChannel` 就不在这儿介绍了，对 Java NIO 这块不熟悉的话建议阅读 [Java NIO](http://tutorials.jenkov.com/java-nio/index.html)，大道至简，非常赞的教程。

`Acceptor` 中的 `processors` 在上一篇说过，网络线程池，在此也不过多介绍。

下面继续分析 `Acceptor` 的各个功能方法，先看下对 processor 的新增和移除操作：

``` scala
private[network] def addProcessors(newProcessors: Buffer[Processor], processorThreadPrefix: String): Unit = synchronized {
  // 将一批processor添加到线程池
  processors ++= newProcessors
  // 启动这批新添加的processor
  if (processorsStarted.get)
    startProcessors(newProcessors, processorThreadPrefix)
}

// 启动一批processor
private def startProcessors(processors: Seq[Processor], processorThreadPrefix: String): Unit = synchronized {
  processors.foreach { processor =>
    // 每个processor是一个runnable，开启一个新的thread去运行runnable
    KafkaThread.nonDaemon(s"${processorThreadPrefix}-kafka-network-thread-$brokerId-${endPoint.listenerName}-${endPoint.securityProtocol}-${processor.id}",
      processor).start()
  }
}
```

可以看到添加 processor 到线程池其实就是启动这批 processor 线程。

``` scala
private[network] def removeProcessors(removeCount: Int, requestChannel: RequestChannel): Unit = synchronized {
  val toRemove = processors.takeRight(removeCount)
  // 移除一批processor
  processors.remove(processors.size - removeCount, removeCount)
  // 关闭这批processor
  toRemove.foreach(_.shutdown())
  // requestChannel也引用了processor，也需要移除引用
  toRemove.foreach(processor => requestChannel.removeProcessor(processor.id))
}

// Processor类
override def shutdown(): Unit = {
  super.shutdown()
  removeMetric("IdlePercent", Map("networkProcessor" -> id.toString))
  metrics.removeMetric(expiredConnectionsKilledCountMetricName)
}

// AbstractServerThread类
def shutdown(): Unit = {
  if (alive.getAndSet(false))
    wakeup() // 唤醒selector
  // ???
  shutdownLatch.await()
}
```

可以看到移除 processor 将会调用 `shutdown` 关闭 processor。最后还有一个 `shutdownLatch.await()`，后面会说到。

上面简单分析了 `Acceptor` 对 processor 的管理相关方法。作为一个 `Runnable`，我们重点需要分析的其实是 `run` 方法，即线程执行的代码：

``` scala
def run(): Unit = {
  // selector注册accept事件
  serverChannel.register(nioSelector, SelectionKey.OP_ACCEPT)
  // Acceptor优雅启动完毕
  startupComplete()
  try {
    var currentProcessorIndex = 0
    
    // 循环不断获取新连接
    while (isRunning) {
      try {
        // 获取就绪事件
        // 最多等500ms，以便当isRunning=false的时候及时结束循环
        val ready = nioSelector.select(500)
        if (ready > 0) {
          val keys = nioSelector.selectedKeys()
          val iter = keys.iterator()
          // 处理这些已就绪的事件
          while (iter.hasNext && isRunning) {
            try {
              val key = iter.next
              iter.remove()
              if (key.isAcceptable) {
                accept(key).foreach { socketChannel =>
                  var retriesLeft = synchronized(processors.length)
                  var processor: Processor = null
                  do {
                    retriesLeft -= 1
                    // Acceptor使用round-robin轮询算法，选择下一个processor处理该连接
                    processor = synchronized {
                      currentProcessorIndex = currentProcessorIndex % processors.length
                      processors(currentProcessorIndex)
                    }
                    currentProcessorIndex += 1
                    // 如果processor的内部队列已经满了，并且不阻塞的话，继续尝试下一个processor
                  } while (!assignNewConnection(socketChannel, processor, retriesLeft == 0))
                }
              } else
                // Acceptor只负责处理accept事件
                throw new IllegalStateException("Unrecognized key state for acceptor thread.")
            } catch {
              case e: Throwable => error("Error while accepting connection", e)
            }
          }
        }
      }
      catch {
        case e: ControlThrowable => throw e
        // 对异常静默处理，继续接收新的请求
        case e: Throwable => error("Error occurred", e)
      }
    }
  } finally {
    debug("Closing server socket and selector.")
    // 关闭selector和channel
    CoreUtils.swallow(serverChannel.close(), this, Level.ERROR)
    CoreUtils.swallow(nioSelector.close(), this, Level.ERROR)
    // Acceptor优雅关闭完毕
    shutdownComplete()
  }
}

private def accept(key: SelectionKey): Option[SocketChannel] = {
  // 获取SocketChannel
  val serverSocketChannel = key.channel().asInstanceOf[ServerSocketChannel]
  val socketChannel = serverSocketChannel.accept()
  try {
    // 如果是总连接数超过最大限制或者listener的连接数超过最大限制，一般是临时的资源紧张，此时阻塞等待
    // 如果是请求ip对应的连接数超过最大限制，为了防止客户端滥用，此时会抛异常
    connectionQuotas.inc(endPoint.listenerName, socketChannel.socket.getInetAddress, blockedPercentMeter)
    // 设置为非阻塞I/O
    socketChannel.configureBlocking(false)
    // 禁用TCP Nagle算法，提高小包的传输效率
    socketChannel.socket().setTcpNoDelay(true)
    // 启用TCP长连接
    socketChannel.socket().setKeepAlive(true)
    // 设置TCP发送缓冲区大小
    if (sendBufferSize != Selectable.USE_DEFAULT_BUFFER_SIZE)
      socketChannel.socket().setSendBufferSize(sendBufferSize)
    // 返回该连接
    Some(socketChannel)
  } catch {
    case e: TooManyConnectionsException =>
      info(s"Rejected connection from ${e.ip}, address already has the configured maximum of ${e.count} connections.")
      // 关闭连接
      close(endPoint.listenerName, socketChannel)
      None
  }
}
```

这段代码中的 `nioSelector.selectedKeys()` 还有 `iter.remove()` 等，这些都只是 NIO Selector 的模板代码，建议把上面推荐的 Java NIO 入门教程看一遍，就很好懂了。这段代码还有几个值得讲一下的点：

首先是 `startupComplete` 以及 `shutdownComplete` （关于 `CountDownLatch` 默认读者已经知道是什么东西，如不会请自行查阅用法），这两个方法实现线程的优雅启动与关闭，优雅启动和关闭在服务端开发中十分常见。所谓“优雅”其实很好理解，我们脱离代码，将“优雅”抽象成一种通用的解释：想象有两个线程，一个主线程一个子线程，主线程可能会与子线程通信交互。主线程启动子线程后，需要等待子线程初始化完成后才能与其通信，因此这里的“等待子线程完成初始化”就是一种“优雅启动”，想象一下如果没有优雅启动，万一子线程初始化资源时失败并退出了，而主线程毫无感知并且直接尝试与子线程进行通信，很有可能出现意想不到的 bug。同理，“优雅关闭”是指等待子线程完成资源的回收工作再进行关闭。另外，光从线程角度来看，说白了不过是一种线程同步方式而已。综上，代码非常简单就不多说了。

其次，`assignNewConnection` 是将连接放到 processor 中的内部队列等待处理，如果队列满了就尝试下一个 processor。如果前 n-1 个 processor 的队列都满了，就阻塞在最后一个 processor 上，直到能放进去为止。

最后，客户端 IP 对应的连接数超过最大限制时，为了防止客户端滥用，会直接抛异常，拒绝连接。

## Processor

通过上面的分析，我们知道 `Acceptor` 用于接收连接建立请求。本节介绍的 `Processor` 则是负责创建连接、分发请求还有发送响应的组件，`Processor` 干了非常多事情。首先来看一下 `Processor` 的类定义：

``` scala
private[kafka] class Processor(
  val id: Int,
  time: Time,
  // 单个请求的最大字节数
  maxRequestSize: Int,
  requestChannel: RequestChannel,
  // 控制最大连接数
  connectionQuotas: ConnectionQuotas,
  // 连接最大空闲时间
  connectionsMaxIdleMs: Long,
  // 认证失败后的延迟时间，防止暴力破解攻击
  failedAuthenticationDelayMs: Int,
  // 标识此处理器属于哪个网络监听器
  listenerName: ListenerName,
  // 安全协议类型
  securityProtocol: SecurityProtocol,
  // 包含broker的各种配置参数
  config: KafkaConfig,
  metrics: Metrics,
  // 管理认证相关的凭证信息
  credentialProvider: CredentialProvider,
  // 内存池，用于高效管理请求和响应的内存分配
  memoryPool: MemoryPool,
  // 提供统一的日志记录功能
  logContext: LogContext,
  connectionQueueSize: Int = ConnectionQueueSize
) extends AbstractServerThread(connectionQuotas) with KafkaMetricsGroup {
  // 待处理连接队列
  private val newConnections = new ArrayBlockingQueue[SocketChannel](connectionQueueSize)
  // 正在发送的响应
  private val inflightResponses = mutable.Map[String, RequestChannel.Response]()
  // 待处理响应队列
  private val responseQueue = new LinkedBlockingDeque[RequestChannel.Response]()
  
  // kafka对NIO Selector的封装
  private val selector = createSelector(
    ChannelBuilders.serverChannelBuilder(listenerName,
      listenerName == config.interBrokerListenerName,
      securityProtocol,
      config,
      credentialProvider.credentialCache,
      credentialProvider.tokenCache,
      time,
      logContext))
}
```

可以看到 `Processor` 大部分的成员都比较好理解，但是需要注意以下几个成员：

- `selector`，这个是 kafka 对 Java NIO Selector 的二次封装，主要是封装了网络 I/O 事件的处理、安全协议支持等。为了与 Java NIO Selector 进行区分，我们称其为 KSelector。由于 KSelector 比较复杂，另起了一篇文章进行剖析，详见[kafka源码阅读（7）-KSelector](https://nos-ae.github.io/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB7-kselector/)。总之，我们知道，Acceptor 和 Processor 都会拥有自己的 selector。
- `inflightResponses` 不同于 `responseQueue`，后者是为了排队这些待发送的响应，而有些 Response 回调函数需要在响应发送给客户端之后才触发，因此额外需要一个 `inflightResponses` 来暂存正在发送的响应，以便发送成功后执行回调逻辑。

接下来我们继续分析 `Processor` 线程的方法。`Processor` 的众多方法都是由 `run` 方法展开调用的，因此我们直接分析 `run` 方法：

``` scala
override def run(): Unit = {
  // Processor优雅启动完毕
  startupComplete()
  try {
    while (isRunning) {
      try {
        // 处理newConnection队列中的新连接
        configureNewConnections()
        // 处理responseQueue队列中的响应，发送给客户端
        processNewResponses()
        // 基于I/O多路复用，获取就绪的I/O事件
        poll()
        // 处理客户端请求
        processCompletedReceives()
        // 处理已完成的响应
        processCompletedSends()
        // 处理因发送失败导致的客户端连接断开
        processDisconnected()
        // 检查并关闭超出配额的连接
        closeExcessConnections()
      } catch {
        case e: Throwable => processException("Processor got uncaught exception.", e)
      }
    }
  } finally {
    debug(s"Closing selector - processor $id")
    // 关闭底层资源
    CoreUtils.swallow(closeAll(), this, Level.ERROR)
    // Processor优雅关闭完毕
    shutdownComplete()
  }
}
```

### configureNewConnections

`Processor` 的 `run` 定义了一套网络 I/O 的处理流程，流程中的各个步骤封装在相应方法中，首先是 `configureNewConnections`，这个方法主要是通过 kselector 注册连接的读 I/O 事件：

``` scala
private def configureNewConnections(): Unit = {
  var connectionsProcessed = 0
  // 由于Acceptor会一直accept新连接，因此需要限制每次处理连接的数量为connectionQueueSize
  while (connectionsProcessed < connectionQueueSize && !newConnections.isEmpty) {
    val channel = newConnections.poll()
    try {
      debug(s"Processor $id listening to new connection from ${channel.socket.getRemoteSocketAddress}")
      // 将该新连接交由kselector处理，实际上是注册OP_READ事件
      // 每个连接在kselector中使用ID唯一标识（TCP四元组+唯一自增序号）
      selector.register(connectionId(channel.socket), channel)
      connectionsProcessed += 1
    } catch {
      case e: Throwable =>
        val remoteAddress = channel.socket.getRemoteSocketAddress
        // need to close the channel here to avoid a socket leak.
        close(listenerName, channel)
        processException(s"Processor $id closed connection from $remoteAddress", e)
    }
  }
}
```

### processNewResponses

`processNewResponses` 用于处理响应队列中的响应：

``` scala
private def processNewResponses(): Unit = {
  var currentResponse: RequestChannel.Response = null
  while ({currentResponse = dequeueResponse(); currentResponse != null}) {
    val channelId = currentResponse.request.context.connectionId
    try {
      currentResponse match {
        case response: NoOpResponse =>
          updateRequestMetrics(response)
          trace(s"Socket server received empty response to send, registering for read: $response")
          // 无需发送响应给客户端，因此需要取消通道静音，读取更多请求到socket buffer中
          // 实际上会注册OP_READ事件
          handleChannelMuteEvent(channelId, ChannelMuteEvent.RESPONSE_SENT)
          tryUnmuteChannel(channelId)

        case response: SendResponse =>
          // 发送响应，实际上是注册OP_WRITE事件
          sendResponse(response, response.responseSend)
        case response: CloseConnectionResponse =>
          // 关闭连接
          updateRequestMetrics(response)
          trace("Closing socket connection actively according to the response code.")
          close(channelId)
        case _: StartThrottlingResponse =>
          // 开始对连接限流
          handleChannelMuteEvent(channelId, ChannelMuteEvent.THROTTLE_STARTED)
        case _: EndThrottlingResponse =>
          // 结束对连接限流
          handleChannelMuteEvent(channelId, ChannelMuteEvent.THROTTLE_ENDED)
          tryUnmuteChannel(channelId)
        case _ =>
          throw new IllegalArgumentException(s"Unknown response type: ${currentResponse.getClass}")
      }
    } catch {
      case e: Throwable =>
        processChannelException(channelId, s"Exception while processing response for $channelId", e)
    }
  }
}
```

可见 `processNewResponses` 只是处理了暂存在队列中的响应，这些响应最初其实是由 `KafkaApi` 完成对请求的处理之后生成响应存到队列中的。在 `processNewResponses` 中的几种响应类型在上一篇博客中已经说过，不再多说。另外这里出现了一些陌生的概念，比如 通道 `KafkaChannel`、静音 `mute` 等，通道用来封装一个网络连接，隐藏了底层的网络通信细节，而静音和限流都是通道提供的功能，静音期间，服务器不会从该通道读取更多请求，但会继续处理已接收的请求。这些概念目前知道大概的意思就够了。

### poll

`poll` 用于获取就绪的 I/O 事件并处理。这个方法只有两行，复杂度都集中在 kselector 的 `poll` 方法上，暂时先跳过：

``` scala
private def poll(): Unit = {
  val pollTimeout = if (newConnections.isEmpty) 300 else 0
  try selector.poll(pollTimeout)
  catch {
    case e @ (_: IllegalStateException | _: IOException) =>
      error(s"Processor $id poll failed", e)
  }
}
```

### processCompletedReceives

`processCompletedReceives` 用于处理经过 `poll` 读取到的网络请求，将其包装成 `Request` 发送给 `RequestChannel` 等待处理：

``` scala
private def processCompletedReceives(): Unit = {
  // 遍历kselector中已完成的接收，这些接收是在poll中读取的
  selector.completedReceives.asScala.foreach { receive =>
    try {
      openOrClosingChannel(receive.source) match {
        case Some(channel) =>
          // 获取header
          val header = RequestHeader.parse(receive.payload)
          if (header.apiKey == ApiKeys.SASL_HANDSHAKE && channel.maybeBeginServerReauthentication(receive, nowNanosSupplier))
            trace(s"Begin re-authentication: $channel")
          else {
            val nowNanos = time.nanoseconds()
            if (channel.serverAuthenticationSessionExpired(nowNanos)) {
              // 如果会话的身份认证已经过期，则断开连接
              debug(s"Disconnecting expired channel: $channel : $header")
              close(channel.id)
              expiredConnectionsKilledCount.record(null, 1, 0)
            } else {
              val connectionId = receive.source
              // 构建Request
              val context = new RequestContext(header, connectionId, channel.socketAddress,
                channel.principal, listenerName, securityProtocol,
                channel.channelMetadataRegistry.clientInformation)
              val req = new RequestChannel.Request(processor = id, context = context,
                startTimeNanos = nowNanos, memoryPool, receive.payload, requestChannel.metrics)
              // KIP-511: 收集ApiVersionsRequest中的客户端名称以及版本，以增强运维对客户端的洞察
              if (header.apiKey == ApiKeys.API_VERSIONS) {
                val apiVersionsRequest = req.body[ApiVersionsRequest]
                if (apiVersionsRequest.isValid) {
                  channel.channelMetadataRegistry.registerClientInformation(new ClientInformation(
                    apiVersionsRequest.data.clientSoftwareName,
                    apiVersionsRequest.data.clientSoftwareVersion))
                }
              }
              // 将Request发送到RequestChannel待处理
              requestChannel.sendRequest(req)
              // 将通道静音，等待请求处理完毕后再继续接收新的请求，底层实现是注销OP_READ
              selector.mute(connectionId)
              handleChannelMuteEvent(connectionId, ChannelMuteEvent.REQUEST_RECEIVED)
            }
          }
        case None =>
          throw new IllegalStateException(s"Channel ${receive.source} removed from selector before processing completed receive")
      }
    } catch {
      case e: Throwable =>
        processChannelException(receive.source, s"Exception while processing request from ${receive.source}", e)
    }
  }
  // 清除这些已经处理的请求
  selector.clearCompletedReceives()
}
```

需要注意的是，每个请求发送到 `RequestChannel` 之后，会 mute 请求所在的通道，以保证同一客户端的请求之间的处理顺序，如果不 mute 的话，后续请求也会被放到请求队列中，最终被 KafkaRequestHandler 并行处理，这将破坏请求的处理顺序。

### processCompletedSends

`processCompletedSends` 这个方法用于处理已经完成发送的响应：

``` scala
private def processCompletedSends(): Unit = {
  // 遍历kselector中已经完成的发送，
  selector.completedSends.asScala.foreach { send =>
    try {
      // 因为响应已经发送完毕，因此将该响应从inflightResponses中移除
      val response = inflightResponses.remove(send.destination).getOrElse {
        throw new IllegalStateException(s"Send for ${send.destination} completed, but not in `inflightResponses`")
      }
      updateRequestMetrics(response)

      // 执行响应完成后的回调逻辑
      response.onComplete.foreach(onComplete => onComplete(send))

      // 通道取消静音
      handleChannelMuteEvent(send.destination, ChannelMuteEvent.RESPONSE_SENT)
      tryUnmuteChannel(send.destination)
    } catch {
      case e: Throwable => processChannelException(send.destination,
        s"Exception while processing completed send to ${send.destination}", e)
    }
  }
  selector.clearCompletedSends()
}
```

可以看到，与 `processCompletedReceives` 中收到请求时对通道静音相对应，响应完成发送之后需要取消对通道的静音。

### processDisconnected

`processDisconnected` 方法用于处理断开的连接：

``` scala
private def processDisconnected(): Unit = {
  // 遍历kselector中所有已经断开的连接
  selector.disconnected.keySet.asScala.foreach { connectionId =>
    try {
      val remoteHost = ConnectionId.fromString(connectionId).getOrElse {
        throw new IllegalStateException(s"connectionId has unexpected format: $connectionId")
      }.remoteHost
      // 断开连接中正在发送的响应也从inflightResponses移除
      inflightResponses.remove(connectionId).foreach(updateRequestMetrics)
      // 更新连接配额
      connectionQuotas.dec(listenerName, InetAddress.getByName(remoteHost))
    } catch {
      case e: Throwable => processException(s"Exception while processing disconnection of $connectionId", e)
    }
  }
}
```

### closeExcessConnections

`closeExcessConnections` 方法用于当总连接数超出配额时强行关闭其中一个连接。优先关闭正处于关闭状态的连接，其次是空闲时间最长的连接，如果都没有的话，关闭任意一个连接。所谓“正处于关闭状态的连接”，是一种优雅关闭，目的是处理该连接上已接收但还未处理完毕的请求，而强行关闭就是将这些连接直接关闭，不再等这些请求处理完毕。

``` scala
private def closeExcessConnections(): Unit = {
  // 检查总连接数是否超出配额
  if (connectionQuotas.maxConnectionsExceeded(listenerName)) {
    // 获取应该被关闭的连接
    val channel = selector.lowestPriorityChannel()
    if (channel != null)
    	// 关闭连接
      close(channel.id)
  }
}

private def close(connectionId: String): Unit = {
  openOrClosingChannel(connectionId).foreach { channel =>
    debug(s"Closing selector connection $connectionId")
    val address = channel.socketAddress
    // 更新连接配额
    if (address != null)
      connectionQuotas.dec(listenerName, address)
    // 强行关闭连接
    selector.close(connectionId)

    // 将连接上发送中的响应从inflightResponses移除
    inflightResponses.remove(connectionId).foreach(response => updateRequestMetrics(response))
  }
}
```

## 总结
本篇我们深入解析了 Kafka 网络通信的关键组件 —— SocketServer，重点关注了其中的 Acceptor 与 Processor 两大核心线程。可以看到，Kafka 采用了典型的 Reactor 模式，通过 Acceptor 线程专职接收连接请求、将连接分发至 Processor，而 Processor 再负责网络读写、请求转发、响应发送等复杂逻辑。

值得关注的是，为了简化 Processor 对连接的处理逻辑，kafka 将 NIO Selector 抽取出来封装到 KSelector 以提供复杂的 I/O 操作、将 SocketChannel 封装到 KafkaChannel 中实现底层传输逻辑以及维护通道的状态，这些内容我们在下一篇[kafka源码阅读（7）-KSelector](https://nos-ae.github.io/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB7-kselector/)中进行分析。


## 参考

[Kafka源码阅读01: 网络层阅读之服务器的启动 - BewareMyPower的博客](https://bewaremypower.github.io/2019/09/18/Kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB01-%E7%BD%91%E7%BB%9C%E5%B1%82%E9%98%85%E8%AF%BB%E4%B9%8B%E6%9C%8D%E5%8A%A1%E5%99%A8%E7%9A%84%E5%90%AF%E5%8A%A8/)

极客时间《Kafka核心源码解读》——胡夕

[TCP选项之SO_RCVBUF和SO_SNDBUF](https://www.cnblogs.com/kex1n/p/7801343.html)

[Java NIO](http://tutorials.jenkov.com/java-nio/index.html)