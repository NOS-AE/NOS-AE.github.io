---
date: 2025-06-11T18:42:13+08:00
title: Kafka源码阅读（9）-SocketServer之DataPlane与ControlPlane
tags: [kafka,mq,源码]
categories: [源码阅读,kafka]
draft: true
---

在本系列的[kafka源码阅读（6）-SocketServer 之 Processor 与 Acceptor](https://nosae.top/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB6-socketserver/)中，主要介绍了负责实现网络编程 reactor 模式的 Acceptor 以及 Processor，对于 SocketServer 如何使用它们并没有介绍到，那么这篇将真正揭开 SocketServer 的神秘面纱。

## 总览

不知从何说起，先把 SocketServer 类的官方注释翻译一下：

> SocketServer 负责处理 kafka 的网络连接、请求和响应。并且将请求类型分为数据类请求以及控制类请求，支持分别用两种类型的请求平面来处理这些请求：
>
> 1. **数据平面 Data Plane**：
>    - 职责：负责处理来自客户端的数据类型请求（如果没开启控制平面，则数据平面将处理所有请求）。
>    - 线程模型：每个 listener 对应 1 个 Acceptor 线程（可以配置多个 listener），1 个 Acceptor 对应 N 个 Processor 线程，Acceptor 接收到的每条连接交由其中 1 个 Processor 进行处理。另外还有 M 个 Handler 线程负责处理连接上发来的请求，得到响应后交回给 Processor 去返回给客户端，注意这 M 个 Handler 并没有与其它线程、连接或者请求有什么对应关系，是整个数据平面中公共的处理请求的线程池。
> 2. **控制平面 Control Plane**：
>    - 职责：负责处理来自 controller 的控制类型请求，如上所述，控制平面是可选的。
>    - 线程模型：整个控制平面只有 1 个 Acceptor 线程负责接建立连接，并且只有 1 个 Processor 线程负责处理所有连接。另外也只有 1 个 Handler 线程来处理所有请求。相比数据平面的线程模型比较“寒酸”，因为控制类型请求的数量远小于数据类型的请求。

从注释的开始我们就看出 SocketServer 基本负责整个 kafka 的网络请求处理，请求类型分为数据类请求和控制类请求，并且出于[一些原因（KAFKA-4453）](https://issues.apache.org/jira/browse/KAFKA-4453)，ServerSocket 分别用数据平面和控制平面来处理这些请求，以保证控制类请求得到优先处理（不禁想起文件传输协议 FTP 也将连接分成了控制连接和数据连接）。更通俗易懂地说，你可以将数据平面和控制平面这两个东西看成两个逻辑请求队列，并有两套并行处理的处理器对队列中的请求进行处理。

上面说的 listener 其实就是 endpoint。endpoint 由主机、端口以及安全协议组成：

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

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20250614142837564.png" alt="image-20250614142837564" style="zoom:50%;" />

这个图可以和剖析 Processor 与 Acceptor 那篇中的图对比着观看。

## 类定义

为了便于注释，我把构造函数的成员也放到大括号中。

``` scala
class SocketServer(
) extends Logging with KafkaMetricsGroup with BrokerReconfigurable {
  // 基础配置相关
  val config: KafkaConfig // kafka服务器配置
  val time: Time
  val credentialProvider: CredentialProvider
  private val logContext = new LogContext(s"[SocketServer brokerId=${config.brokerId}] ")
  this.logIdent = logContext.logPrefix

  // 内存管理相关
  private val memoryPool = if (config.queuedMaxBytes > 0) new SimpleMemoryPool(config.queuedMaxBytes, config.socketRequestMaxBytes, false, memoryPoolSensor) else MemoryPool.NONE
  
  // 数据平面相关
  private val maxQueuedRequests = config.queuedMaxRequests
  private val dataPlaneProcessors = new ConcurrentHashMap[Int, Processor]()
  private[network] val dataPlaneAcceptors = new ConcurrentHashMap[EndPoint, Acceptor]()
  val dataPlaneRequestChannel = new RequestChannel(maxQueuedRequests, DataPlaneMetricPrefix)
  
  // 控制平面相关
  private var controlPlaneProcessorOpt : Option[Processor] = None
  private[network] var controlPlaneAcceptorOpt : Option[Acceptor] = None
  val controlPlaneRequestChannelOpt: Option[RequestChannel] = config.controlPlaneListenerName.map(_ => new RequestChannel(20, ControlPlaneMetricPrefix))

  // 连接管理相关
  private var nextProcessorId = 0
  private var connectionQuotas: ConnectionQuotas = _
  private var stoppedProcessingRequests = false
  
  // 监控指标相关
  val metrics: Metrics
  private val memoryPoolSensor = metrics.sensor("MemoryPoolUtilization")
  memoryPoolSensor.add(new Meter(TimeUnit.MILLISECONDS, memoryPoolDepletedPercentMetricName, memoryPoolDepletedTimeMetricName))
  private val memoryPoolDepletedPercentMetricName = metrics.metricName("MemoryPoolAvgDepletedPercent", MetricsGroup)
  private val memoryPoolDepletedTimeMetricName = metrics.metricName("MemoryPoolDepletedTimeTotal", MetricsGroup)
}
```

在成员对象比较多的时候我喜欢分类注释，这样一下就能看出来哪些成员需要重点关注的。很明显，数据平面和控制平面相关的成员是我们重点关注的，可以看到全部是之前讲过的组件，包括 RequestChannel、Processor、Acceptor。需要注意的是，因为控制面是可选的，所以控制面相关的对象全都是 Option[xxx]。

## 启动SocketServer

`startup` 方法用于启动 SocketServer，在生产代码中的 KafkaServer 中我们可以看到，用于控制启动 SocketServer 时是否启动 Processor 的 startupProcessor 参数的永远是 false，这里是涉及到组件启动顺序的问题，即需要等整个服务端初始化的最后，KafkaServer 才会去调用 startControlPlaneProcessor 和 startDataPlaneProcessors 来启动这些 Processor：

``` scala
def startup(startupProcessors: Boolean = true): Unit = {
  this.synchronized {
    connectionQuotas = new ConnectionQuotas(config, time)
    // 创建控制面的acceptor和processor
    createControlPlaneAcceptorAndProcessor(config.controlPlaneListener)
    // 创建数据平面的acceptor和processor
    createDataPlaneAcceptorsAndProcessors(config.numNetworkThreads, config.dataPlaneListeners)
    // 忽略
    if (startupProcessors) {
      startControlPlaneProcessor(Map.empty)
      startDataPlaneProcessors(Map.empty)
    }
  }

  // 剩余的代码是监控指标相关
  // ...
}
```

在 `KafkaServer` 中对 SocketServer 启动的相关代码如下，可以看到 Processor 是在最后才启动的：

``` scala
def startup(): Unit = {
  // ...
  
  socketServer = new SocketServer(config, metrics, time, credentialProvider)
  socketServer.startup(startupProcessors = false)
  
  // ...
  
  // 延迟启动Processor
  socketServer.startControlPlaneProcessor(authorizerFutures)
  socketServer.startDataPlaneProcessors(authorizerFutures)
  
  // ...
  
  info("started")
}
```



##  控制平面

接下来控制面的创建和启动，首先是创建，主要包括：

1. 创建并启动 acceptor
2. 创建 processor

``` scala
private def createControlPlaneAcceptorAndProcessor(endpointOpt: Option[EndPoint]): Unit = synchronized {
  // 注意这里不是遍历，而是有listener的话才会进入代码块
  endpointOpt.foreach { endpoint =>
    // 添加listener到连接配额管理中
    connectionQuotas.addListener(config, endpoint.listenerName)
    // 创建acceptor
    val controlPlaneAcceptor = createAcceptor(endpoint, ControlPlaneMetricPrefix)
    // 创建processor
    val controlPlaneProcessor = newProcessor(nextProcessorId, controlPlaneRequestChannelOpt.get, connectionQuotas, endpoint.listenerName, endpoint.securityProtocol, memoryPool)
    controlPlaneAcceptorOpt = Some(controlPlaneAcceptor)
    controlPlaneProcessorOpt = Some(controlPlaneProcessor)
    val listenerProcessors = new ArrayBuffer[Processor]()
    listenerProcessors += controlPlaneProcessor
    // 添加该processor到RequestChannel中
    controlPlaneRequestChannelOpt.foreach(_.addProcessor(controlPlaneProcessor))
    nextProcessorId += 1
    // 添加该processor到acceptor中
    controlPlaneAcceptor.addProcessors(listenerProcessors, ControlPlaneThreadPrefix)
    // 启动acceptor，作为非守护线程
    KafkaThread.nonDaemon(s"${ControlPlaneThreadPrefix}-kafka-socket-acceptor-${endpoint.listenerName}-${endpoint.securityProtocol}-${endpoint.port}", controlPlaneAcceptor).start()
    // 等待acceptor优雅启动完毕
    controlPlaneAcceptor.awaitStartup()
    info(s"Created control-plane acceptor and processor for endpoint : $endpoint")
  }
}
```

注意这里 Acceptor 在创建时就已经启动了，而 Processor 是在后面 KafkaServer 初始化完成后再启动，这个其实是会造成 bug 的（参见[KAFKA-9796](https://issues.apache.org/jira/browse/KAFKA-9796)）。在更新一版的 kafka 中，其实是 Acceptor 和 Processor 都放在后面启动，不过这并不影响我们继续分析。

接下来看一下控制平面 processor 的启动，也是启动非守护线程：

``` scala
def startControlPlaneProcessor(authorizerFutures: Map[Endpoint, CompletableFuture[Void]] = Map.empty): Unit = synchronized {
  controlPlaneAcceptorOpt.foreach { controlPlaneAcceptor =>
    waitForAuthorizerFuture(controlPlaneAcceptor, authorizerFutures)
    controlPlaneAcceptor.startProcessors(ControlPlaneThreadPrefix)
    info(s"Started control-plane processor for the control-plane acceptor")
  }
}

// Acceptor.startProcessors
private[network] def startProcessors(processorThreadPrefix: String): Unit = synchronized {
  if (!processorsStarted.getAndSet(true)) {
    startProcessors(processors, processorThreadPrefix)
  }
}

// Acceptor.startProcessors
private def startProcessors(processors: Seq[Processor], processorThreadPrefix: String): Unit = synchronized {
  processors.foreach { processor =>
    KafkaThread.nonDaemon(s"${processorThreadPrefix}-kafka-network-thread-$brokerId-${endPoint.listenerName}-${endPoint.securityProtocol}-${processor.id}",
      processor).start()
  }
}
```

## 数据平面

数据平面的创建与控制平面基本没什么区别，也是创建 acceptor 、processor 以及 RequestChannel：

``` scala
private def createDataPlaneAcceptorsAndProcessors(dataProcessorsPerListener: Int,
                                                  endpoints: Seq[EndPoint]): Unit = synchronized {
  // 遍历所有listener
  endpoints.foreach { endpoint =>
    // 添加listener到连接配额管理中
    connectionQuotas.addListener(config, endpoint.listenerName)
    // 创建acceptor
    val dataPlaneAcceptor = createAcceptor(endpoint, DataPlaneMetricPrefix)
    // 创建processors
    addDataPlaneProcessors(dataPlaneAcceptor, endpoint, dataProcessorsPerListener)
    // 启动acceptor
    KafkaThread.nonDaemon(s"data-plane-kafka-socket-acceptor-${endpoint.listenerName}-${endpoint.securityProtocol}-${endpoint.port}", dataPlaneAcceptor).start()
    dataPlaneAcceptor.awaitStartup()
    dataPlaneAcceptors.put(endpoint, dataPlaneAcceptor)
    info(s"Created data-plane acceptor and processors for endpoint : $endpoint")
  }
}

// 为该processor创建N个processor，N=numNetworkThreads
private def addDataPlaneProcessors(acceptor: Acceptor, endpoint: EndPoint, newProcessorsPerListener: Int): Unit = synchronized {
  val listenerName = endpoint.listenerName
  val securityProtocol = endpoint.securityProtocol
  val listenerProcessors = new ArrayBuffer[Processor]()
  for (_ <- 0 until newProcessorsPerListener) {
    val processor = newProcessor(nextProcessorId, dataPlaneRequestChannel, connectionQuotas, listenerName, securityProtocol, memoryPool)
    listenerProcessors += processor
    dataPlaneRequestChannel.addProcessor(processor)
    nextProcessorId += 1
  }
  listenerProcessors.foreach(p => dataPlaneProcessors.put(p.id, p))
  acceptor.addProcessors(listenerProcessors, DataPlaneThreadPrefix)
}
```

然后是数据平面 processor 的启动：

``` scala
def startDataPlaneProcessors(authorizerFutures: Map[Endpoint, CompletableFuture[Void]] = Map.empty): Unit = synchronized {
  // 先处理用于Broker间内部通信的Acceptor
  val interBrokerListener = dataPlaneAcceptors.asScala.keySet
    .find(_.listenerName == config.interBrokerListenerName)
    .getOrElse(throw new IllegalStateException(s"Inter-broker listener ${config.interBrokerListenerName} not found, endpoints=${dataPlaneAcceptors.keySet}"))
  val orderedAcceptors = List(dataPlaneAcceptors.get(interBrokerListener)) ++
    dataPlaneAcceptors.asScala.filterKeys(_ != interBrokerListener).values
  // 启动Processor
  orderedAcceptors.foreach { acceptor =>
    val endpoint = acceptor.endPoint
    debug(s"Wait for authorizer to complete start up on listener ${endpoint.listenerName}")
    waitForAuthorizerFuture(acceptor, authorizerFutures)
    debug(s"Start processors on listener ${endpoint.listenerName}")
    acceptor.startProcessors(DataPlaneThreadPrefix)
  }
  info(s"Started data-plane processors for ${dataPlaneAcceptors.size} acceptors")
}
```

## 总结

这篇的内容并不难，重点在于体现 kafka 网络层的架构设计，即将数据类型与控制类型的请求分离，将它们在不同的通道上进行传输，保证 kafka 能及时处理控制类请求，最大程度避免出现比如「某个在请求队列中的控制请求由于h前面有大量数据请求，导致这些数据请求都基于过时的状态进行处理」的情况。
