---
date: 2025-06-27T12:16:37+08:00
title: Kafka源码阅读（11）-Controller之元数据与通信
categories: [源码阅读,kafka]
tags: [kafka,mq,源码]
draft: false
---

在本 kafka 系列的源码阅读系列中，之前几篇涵盖了 log 和 network 两大板块。本篇将开启第三板块：controller。首先我们要明确 controller 具体是一个什么东西，它其实是集群中选举出来的某一个 broker。controller 通过与 zookeeper 配合来处理：

1. 分区 leader 选举
2. 元数据同步到其它 broker
4. 处理 broker 上下线事件

controller 的源码位于 core 包的 controller 包下，由于 controller 设计的内容较多，因此会分为几篇来介绍。

本篇目的是过一遍这个包中的 ControllerContex 类，因为这个类保存了 controller 的所有元数据，对它们有所了解后可以方便我们后续去分析各种功能性代码。除此以外本篇还会介绍 ControllerChannelManager，controller 会与每个 broker 建立并维护一个专用的通信通道，这个类则负责管理这些通信通道。

## ControllerContext

说起 kafka 中的元数据，我们知道元数据一般都应该保存在 zk 上，为什么这里说 controller 保存了元数据呢？事实上，集群 broker 是不会与 zk 直接打交道去读写元数据的，而是与 controller 通信来获取和更新最新的元数据，而 controller 又会与 zk 通信来得到最新的元数据，因此 controller 上的元数据可以说是 zk 的副本，zk 才是元数据的源头。明白 broker、controller 以及 zk 对于操作元数据的关系之后，下面来看看 ControllerContext 里都保存了什么元数据。

ControllerContex 一共有 17 个字段，按照我的习惯，给他们分类注释：

``` scala
class ControllerContext {
  // 控制器状态相关
  var epoch: Int = KafkaController.InitialControllerEpoch // 当前epoch，用于表示controller的选举轮次
  var epochZkVersion: Int = KafkaController.InitialControllerEpochZkVersion // epoch在zk中的版本号，用于乐观锁控制
  
  // broker相关
  private var liveBrokers: Set[Broker] = Set.empty // 当前活跃的broker集合
  private var liveBrokerEpochs: Map[Int, Long] = Map.empty // 当前活跃broker id到其epoch的映射
  var shuttingDownBrokerIds: mutable.Set[Int] = mutable.Set.empty // 正在关闭的broker id集合
  val replicasOnOfflineDirs: mutable.Map[Int, Set[TopicPartition]] = mutable.Map.empty // broker id到该broker上离线分区的集合
  
  // topic相关
  var allTopics: Set[String] = Set.empty // 所有topic的集合
  val topicsToBeDeleted = mutable.Set.empty[String] // 待删除topic的集合
  val topicsWithDeletionStarted = mutable.Set.empty[String] // 正在被删除的topic集合
  val topicsIneligibleForDeletion = mutable.Set.empty[String] // 不符合删除条件的topic集合
  
  // partition相关
  val partitionAssignments = mutable.Map.empty[String, mutable.Map[Int, ReplicaAssignment]] // 各主题分区副本的分配情况
  val partitionLeadershipInfo = mutable.Map.empty[TopicPartition, LeaderIsrAndControllerEpoch] // 分区leader、ISR及其控制器epoch的信息
  val partitionsBeingReassigned = mutable.Set.empty[TopicPartition] // 正在进行副本重分配的分区集合
  val partitionStates = mutable.Map.empty[TopicPartition, PartitionState] // 分区状态
  val replicaStates = mutable.Map.empty[PartitionAndReplica, ReplicaState] // 分区副本状态
  var offlinePartitionCount = 0 // 离线分区的数量

  // 监控指标相关
  val stats = new ControllerStats
}
```

下面分别介绍一下几个不好理解的字段。

### offlinePartitionCount



这个字段用于统计离线的分区数量。所谓“离线”指的是该分区当前没有可以用的 leader，需要选举一个新的 leader。分区的状态实际上有如下四种：

1. **NewPartition**：刚创建的新分区，有副本分配但是还没有 ISR 和 leader
2. **OnlinePartition**：正常运行的分区，有 ISR 和 leader
3. **OfflinePartition**：leader 不可用，需要选举一个新的 leader
4. **NonExistentPartition**：分区不存在（从未创建或者已被删除）

### epoch 和 epochZkVersion

- epoch：epoch 是 controller 在整个 Kafka 集群的版本号（类似于 raft 算法中的任期 term），每当发生新的控制器选举时，这个值会递增，epoch 最大的那个节点才是有效的 controller，并且所有 controller 发出的请求（如LeaderAndIsr）都会包含当前的 epoch，这是为了防止发生脑裂的情况，因为整个集群只允许存在一个有效 controller。刚刚说过，controller 保存的元数据相当于 zk 上的副本，epoch 实际上对应于 zk 上的 /controller_epoch 节点的值。

- epochZkVersion：epoch 代表当前 controller 的版本号，那么 epochZkVersion 就代表了当前 epoch 的版本号，对应于 zk 上的 /controller_epoch 节点的值的版本，用于实现 epoch 更新时的乐观锁。比如在 `registerControllerAndIncrementControllerEpoch` 方法中，传入了当前预期的 /controller_epoch 节点的版本，只有当节点的版本号与预期一致时，才会更新成功：

  ``` scala
  // 创建/controller和更新/controller_epoch原子性操作
  val response = retryRequestUntilConnected(
    MultiRequest(Seq(
      CreateOp(ControllerZNode.path, ControllerZNode.encode(controllerId, timestamp), defaultAcls(ControllerZNode.path), CreateMode.EPHEMERAL),
      SetDataOp(ControllerEpochZNode.path, ControllerEpochZNode.encode(newControllerEpoch), expectedControllerEpochZkVersion)))
  )
  ```

  如果版本号不一致，说明已经有其他 controller 更新了 epoch，那么当前 controller 就会放弃或者重试：

  ``` scala
  response.resultCode match {
    // BADVERSION，版本号
    case Code.NODEEXISTS | Code.BADVERSION => checkControllerAndEpoch()
    case Code.OK =>
      val setDataResult = response.zkOpResults(1).rawOpResult.asInstanceOf[SetDataResult]
      (newControllerEpoch, setDataResult.getStat.getVersion)
    case code => throw KeeperException.create(code)
  }
  ```

  再者，在 controller 调用 `triggerControllerMove` 放弃控制权时，调用使用 epochZkVersion 确保只删除自己创建的节点，防止误删其它控制器创建的节点：

  ``` scala
  // triggerControllerMove方法
  val expectedControllerEpochZkVersion = controllerContext.epochZkVersion
  activeControllerId = -1
  onControllerResignation()
  zkClient.deleteController(expectedControllerEpochZkVersion)
  ```

### partitionAssignments

这个字段存储了所有主题分区的副本分配情况，我们可以用这个字段去获取不同维度获取数据。首先它是一个二维 map，内容是 [主题名字 -> [分区 id -> 分区副本分配情况]]。这里分区副本分配情况用 ReplicaAssignment 表示：

``` scala
case class ReplicaAssignment private (
  replicas: Seq[Int],
  addingReplicas: Seq[Int],
  removingReplicas: Seq[Int]
)
```

这里的 replicas 就存储了所有的副本所在 broker id，因为同一个分区的不同副本肯定不在同一个 broker，所以可以用 broker id 来表示一个分区副本。

用这个字段我们可以从不同维度获取我们想要的数据，比如，获取某个 broker 上的所有分区：

``` scala
def partitionsOnBroker(brokerId: Int): Set[TopicPartition] = {
  partitionAssignments.flatMap {
    case (topic, topicReplicaAssignment) => topicReplicaAssignment.filter {
      case (_, partitionAssignment) => partitionAssignment.replicas.contains(brokerId)
    }.map {
      case (partition, _) => new TopicPartition(topic, partition)
    }
  }.toSet
}
```

再比如，获取某个主题的所有分区：

```  scala
def partitionsForTopic(topic: String): collection.Set[TopicPartition] = {
  partitionAssignments.getOrElse(topic, mutable.Map.empty).map {
    case (partition, _) => new TopicPartition(topic, partition)
  }.toSet
}
```

## ControllerChannelManager

controller 作为管理整个 broker 集群的角色，势必要与其他 broker 进行交互，controller 会与每个 broker 建立一个通道进行通信。ControllerChannelManager 这个类就是用于创建与维护这些通信通道的。

### 请求类型

controller 会作为网络通信中的客户端，给其它 broker 发送请求（包括 controller 自己所在的 broker），比如 LeaderAndIsr 请求、StopReplica 请求等，这些请求都在 KafkaApis 都有相应的 handleXXXRequest 方法进行处理：

``` scala
def handle(request: RequestChannel.Request): Unit = {
  try {
    request.header.apiKey match {
      case ApiKeys.LEADER_AND_ISR => handleLeaderAndIsrRequest(request)
      case ApiKeys.STOP_REPLICA => handleStopReplicaRequest(request)
      case ApiKeys.UPDATE_METADATA => handleUpdateMetadataRequest(request)
      
      // ...
```

截止 kafka 2.5 版本，controller 只会向其它 broker 发送上面代码中展示的三种请求，并且它们均属于控制类请求（参考[这篇](https://nosae.top/posts/kafka%E6%BA%90%E7%A0%81%E9%98%85%E8%AF%BB9-socketserver2/)）：

1. **LeaderAndIsr**：用于通知 broker 分区 leader 在哪个 broker 上、ISR 在哪些 broker 上
2. **StopReplica**：用于通知 broker 停止它上面的分区副本，该请求主要使用场景是分区副本迁移和删除主题
3. **UpdateMetadata**：用于向集群中所有 broker 广播最新的集群元数据

这三种请求都继承于 `AbstractControlRequest` 类，并且区别于数据类请求，该控制类请求会包含这三个字段：

``` java
public abstract class AbstractControlRequest extends AbstractRequest {
  public static abstract class Builder<T extends AbstractRequest> extends AbstractRequest.Builder<T> {
    protected final int controllerId; // controller 所在的 broker id
    protected final int controllerEpoch; // controller epoch
    protected final long brokerEpoch; // 目标broker的epoch
  }
}
```

前面俩字段都好理解，最后一个「目标 broker 的 epoch」得说一下。首先目标 broker 指的就是接收该请求的 broker，而 broker epoch 不同于分区的 leader epoch，更不是 controller epoch，而是用于标识 broker 的特定版本的一个长整型值，每当 broker 重新注册到集群时，会获得一个新的 broker epoch，而且这个值是递增的，比如说 broker 宕机重启再次注册时，获得的新 broker epoch 一定比上次注册的 broker epoch 大。再说这个 broker epoch 的作用，其实就是用于判断控制类请求是否已经过时：

``` scala
private def isBrokerEpochStale(brokerEpochInRequest: Long): Boolean = {
  if (brokerEpochInRequest == AbstractControlRequest.UNKNOWN_BROKER_EPOCH) false
  else {
    // 如果当前broker的epoch更大，说明这个请求已经过时
    brokerEpochInRequest < controller.brokerEpoch
  }
}
```

什么时候会出现过时请求的情况呢？当 broker 快速重启后收到了该请求，原文注释：

>   When the broker restarts very quickly, it is possible for this broker to receive request intended for its previous generation so the broker should skip the stale request.

那为什么要拒绝这种过时的请求呢？将 broker 整体简单看成一个状态机的话，控制类请求会将 broker 从状态 A 更改为状态 B，因此状态 A 是该请求所期望该 broker 的状态，但 broker 重启之后可能状态不再是 A 而是 C，所以不能继续处理该请求，而拒绝该请求，否则可能会状态紊乱。

### 请求发送

介绍完请求类型，再来看看请求的发送。ControllerChannelManager 并不会直接发送请求，而是将请求塞到请求队列中，让 RequestSendThread 线程来执行发送操作。可以发现，kafka 除了本身就是「生产 + 存储 + 消费」模型以外，里面很多组件的实现也喜欢用这种方式，比如之前说过的 Processor 和 KafkaHandlerThread，还有这里的 ControllerChannelManager 和 RequestSendThread。

ControllerChannelManager 里最重要的字段是 `brokerStateInfo`：

``` scala
protected val brokerStateInfo = new HashMap[Int, ControllerBrokerStateInfo] // broker id -> ControllerBrokerStateInfo

case class ControllerBrokerStateInfo(
  networkClient: NetworkClient, // 与目标broker通信的网络工具类
  brokerNode: Node, // 目标broker的连接信息
  messageQueue: BlockingQueue[QueueItem], // 请求队列
  requestSendThread: RequestSendThread, // 请求处理线程
  queueSizeGauge: Gauge[Int],
  requestRateAndTimeMetrics: Timer,
  reconfigurableChannelBuilder: Option[Reconfigurable]
)
```

可以看出来，对于每个 broker 都会维护一个请求队列以及处理请求的请求处理线程。

接下来我们依此看下 ControllerChannelManager 的各个方法，都很简单，只有 addNewBroker 相对长点。首先是启动：

``` scala
// 启动ControllerChannelManager
// 其实就是创建ControllerBrokerStateInfo，然后启动所有请求处理线程
def startup() = {
  controllerContext.liveOrShuttingDownBrokers.foreach(addNewBroker)

  brokerLock synchronized {
    brokerStateInfo.foreach(brokerState => startRequestSendThread(brokerState._1))
  }
}

// 添加broker，创建ControllerBrokerStateInfo
private def addNewBroker(broker: Broker): Unit = {
  // 创建请求队列
  val messageQueue = new LinkedBlockingQueue[QueueItem]
  debug(s"Controller ${config.brokerId} trying to connect to broker ${broker.id}")
  // 创建listener和node
  val controllerToBrokerListenerName = config.controlPlaneListenerName.getOrElse(config.interBrokerListenerName)
  val controllerToBrokerSecurityProtocol = config.controlPlaneSecurityProtocol.getOrElse(config.interBrokerSecurityProtocol)
  val brokerNode = broker.node(controllerToBrokerListenerName)
  val logContext = new LogContext(s"[Controller id=${config.brokerId}, targetBrokerId=${brokerNode.idString}] ")
  // 创建网络通信类
  val (networkClient, reconfigurableChannelBuilder) = {
    val channelBuilder = ChannelBuilders.clientChannelBuilder(
      controllerToBrokerSecurityProtocol,
      JaasContext.Type.SERVER,
      config,
      controllerToBrokerListenerName,
      config.saslMechanismInterBrokerProtocol,
      time,
      config.saslInterBrokerHandshakeRequestEnable,
      logContext
    )
    val reconfigurableChannelBuilder = channelBuilder match {
      case reconfigurable: Reconfigurable =>
        config.addReconfigurable(reconfigurable)
        Some(reconfigurable)
      case _ => None
    }
    // 创建KSelector
    // 配置：响应大小无限制、不关闭空闲连接
    val selector = new Selector(
      NetworkReceive.UNLIMITED,
      Selector.NO_IDLE_TIMEOUT_MS,
      metrics,
      time,
      "controller-channel",
      Map("broker-id" -> brokerNode.idString).asJava,
      false,
      channelBuilder,
      logContext
    )
    // 创建网络通信工具类
    val networkClient = new NetworkClient(
      selector,
      new ManualMetadataUpdater(Seq(brokerNode).asJava),
      config.brokerId.toString,
      1,
      0,
      0,
      Selectable.USE_DEFAULT_BUFFER_SIZE,
      Selectable.USE_DEFAULT_BUFFER_SIZE,
      config.requestTimeoutMs,
      ClientDnsLookup.DEFAULT,
      time,
      false,
      new ApiVersions,
      logContext
    )
    (networkClient, reconfigurableChannelBuilder)
  }
  val threadName = threadNamePrefix match {
    case None => s"Controller-${config.brokerId}-to-broker-${broker.id}-send-thread"
    case Some(name) => s"$name:Controller-${config.brokerId}-to-broker-${broker.id}-send-thread"
  }

  val requestRateAndQueueTimeMetrics = newTimer(
    RequestRateAndQueueTimeMetricName, TimeUnit.MILLISECONDS, TimeUnit.SECONDS, brokerMetricTags(broker.id)
  )

  // 创建请求消费线程
  val requestThread = new RequestSendThread(config.brokerId, controllerContext, messageQueue, networkClient,
    brokerNode, config, time, requestRateAndQueueTimeMetrics, stateChangeLogger, threadName)
  requestThread.setDaemon(false)

  val queueSizeGauge = newGauge(QueueSizeMetricName, () => messageQueue.size, brokerMetricTags(broker.id))

  // 创建ControllerStateInfo
  brokerStateInfo.put(broker.id, ControllerBrokerStateInfo(networkClient, brokerNode, messageQueue,
    requestThread, queueSizeGauge, requestRateAndQueueTimeMetrics, reconfigurableChannelBuilder))
}
```

注意创建 listener 的时候，正如我们之前所说的，因为 controller 发送的是控制类请求，在创建 listener 时会优先使用控制平面对应的 listener 配置。

然后是关闭：

``` scala
// 关闭ControllerChannelManager
def shutdown() = {
  brokerLock synchronized {
    brokerStateInfo.values.toList.foreach(removeExistingBroker)
  }
}

private def removeExistingBroker(brokerState: ControllerBrokerStateInfo): Unit = {
  try {
    brokerState.reconfigurableChannelBuilder.foreach(config.removeReconfigurable)
    // 关闭请求处理线程
    brokerState.requestSendThread.shutdown()
    // 关闭网络通信类
    brokerState.networkClient.close()
    // 清空请求队列
    brokerState.messageQueue.clear()
    removeMetric(QueueSizeMetricName, brokerMetricTags(brokerState.brokerNode.id))
    removeMetric(RequestRateAndQueueTimeMetricName, brokerMetricTags(brokerState.brokerNode.id))
    // 移除ControllerBrokerStateInfo
    brokerStateInfo.remove(brokerState.brokerNode.id)
  } catch {
    case e: Throwable => error("Error while removing broker by the controller", e)
  }
}
```

还有 `addBroker` 和 `removeBroker` ，最终都是调用上面说过的 `addNewBroker` 以及 `removeExistingBroker`。

接下来是发送请求：

``` scala
def sendRequest(brokerId: Int, request: AbstractControlRequest.Builder[_ <: AbstractControlRequest], callback: AbstractResponse => Unit = null): Unit = {
  brokerLock synchronized {
    // 获取ControllerBrokerStateInfo
    val stateInfoOpt = brokerStateInfo.get(brokerId)
    stateInfoOpt match {
      case Some(stateInfo) =>
      	// 将请求封装在QueueItem中，放入请求队列
        stateInfo.messageQueue.put(QueueItem(request.apiKey, request, callback, time.milliseconds()))
      case None =>
        warn(s"Not sending request $request to broker $brokerId, since it is offline.")
    }
  }
}
```

其中 QueueItem 就是一个 POJO 类，主要保存了请求的 ApiKeys（请求种类）、RequestBuilder、收到响应后的回调函数。

总的来说，ControllerChannelManager 就做了两件事：

1. 记录每个 broker 相关的请求处理线程、节点连接信息等
2. 将请求放入相应 broker 的请求队列

接下来再分析下请求处理线程都干了什么。

### 请求处理

请求处理线程 RequestSendThread 负责请求处理，这个类继承自 ShutdownableThread 类：

``` scala
class RequestSendThread(
  val controllerId: Int,
  val controllerContext: ControllerContext,
  val queue: BlockingQueue[QueueItem], // 请求队列
  val networkClient: NetworkClient, // 网络通信类
  val brokerNode: Node, // 目标broker的连接信息
  val config: KafkaConfig,
  val time: Time,
  val requestRateAndQueueTimeMetrics: Timer,
  val stateChangeLogger: StateChangeLogger,
  name: String
) extends ShutdownableThread(name = name) {
  // ...
}
```

ShutdownableThread 封装了线程退出等操作，子类只需在 doWork 写上要执行的业务逻辑即可：

``` scala
override def run(): Unit = {
  isStarted = true
  info("Starting")
  try {
    // 不断执行doWork
    while (isRunning)
      doWork()
  } catch {
    case e: FatalExitError =>
      shutdownInitiated.countDown()
      shutdownComplete.countDown()
      info("Stopped")
      Exit.exit(e.statusCode())
    case e: Throwable =>
      if (isRunning)
        error("Error due to", e)
  } finally {
     shutdownComplete.countDown()
  }
  info("Stopped")
}
```

我们直接看到执行逻辑的 doWork 方法：

``` scala
override def doWork(): Unit = {

  def backoff(): Unit = pause(100, TimeUnit.MILLISECONDS)

  // 从请求队列取出
  val QueueItem(apiKey, requestBuilder, callback, enqueueTimeMs) = queue.take()
  requestRateAndQueueTimeMetrics.update(time.milliseconds() - enqueueTimeMs, TimeUnit.MILLISECONDS)

  var clientResponse: ClientResponse = null
  try {
    var isSendSuccessful = false
    while (isRunning && !isSendSuccessful) {
      try {
        // 如果broker还没连接上，就等一会再试试
        if (!brokerReady()) {
          isSendSuccessful = false
          backoff()
        }
        // 发送请求
        else {
          val clientRequest = networkClient.newClientRequest(brokerNode.idString, requestBuilder,
            time.milliseconds(), true)
          clientResponse = NetworkClientUtils.sendAndReceive(networkClient, clientRequest, time)
          isSendSuccessful = true
        }
      } catch {
        case e: Throwable => // if the send was not successful, reconnect to broker and resend the message
          warn(s"Controller $controllerId epoch ${controllerContext.epoch} fails to send request $requestBuilder " +
            s"to broker $brokerNode. Reconnecting to broker.", e)
          networkClient.close(brokerNode.idString)
          isSendSuccessful = false
          backoff()
      }
    }
    if (clientResponse != null) {
      val requestHeader = clientResponse.requestHeader
      val api = requestHeader.apiKey
      // 检查响应是否对应请求的类型
      if (api != ApiKeys.LEADER_AND_ISR && api != ApiKeys.STOP_REPLICA && api != ApiKeys.UPDATE_METADATA)
        throw new KafkaException(s"Unexpected apiKey received: $apiKey")

      val response = clientResponse.responseBody

      stateChangeLogger.withControllerEpoch(controllerContext.epoch).trace(s"Received response " +
        s"${response.toString(requestHeader.apiVersion)} for request $api with correlation id " +
        s"${requestHeader.correlationId} sent to broker $brokerNode")

      // 调用回调函数，进一步处理响应
      if (callback != null) {
        callback(response)
      }
    }
  } catch {
    case e: Throwable =>
      error(s"Controller $controllerId fails to send a request to broker $brokerNode", e)
      // If there is any socket error (eg, socket timeout), the connection is no longer usable and needs to be recreated.
      networkClient.close(brokerNode.idString)
  }
}
```

可以发现请求处理线程只是用 NetworkClient 将请求发送到目标 broker，收到响应后调用回调函数对响应进行处理。

## 总结

本篇作为 controller 的开篇，没有介绍太复杂的内容，只是过了一下 controller 元数据类 ControllerContext 以及 controller 到目标 broker 的请求发送类 ControllerChannelManager。通过 ControllerChannelManager 提供的方法，如添加 broker、移除 broker 等，controller 在集群扩缩容时能够快速地响应到这些变化。

在下一篇中，我们将会介绍 controller 的单线程事件处理器，很简单，其实就是「其它各个线程往事件队列发送事件，最终由单线程进行处理这些事件」。

## 参考

极客时间《Kafka核心源码解读》——胡夕
