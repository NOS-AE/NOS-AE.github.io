---
date: 2025-07-05T12:40:29+08:00
title: Kafka源码阅读（13）-Controller之controller对集群的管理
categories: [源码阅读,kafka]
tags: [kafka,mq,源码]
draft: false
---

> [!note]
>
> 基于开源 kafka 2.5 版本。
>
> 如无特殊说明，文中代码片段将删除 debug 信息、异常触发、英文注释等代码，以便观看核心代码。

本篇将针对 controller 的核心类——KafkaController 进行分析，包括 controller 最开始何被选举出来以及故障转移的、controler 如何管理集群成员、主题等。当然，controller 的作用不止这些，还有一些功能是没讲/没深入讲的，但读者可以借助本篇的内容，对 controller 在整个 kafka 中的作用做到举一反三。

## zookeeper 知识

开始讲源码前先了解一下 zookeeper（以下简称 zk）的一些基本使用，因为 2.5 版本的 kafka 是依赖于 zk 对集群进行管理的，controller 作为集群的控制中枢更是离不开 zk。

zk 是一个分布式协调服务，在分布式系统中用于进行配置管理、分布式同步等，简单来说，你可以将它最核心的功能概括为提供轻量的、配置级别的存储组件。

znode 是 zk 的核心概念之一。在 zk 中，数据以一种类似文件系统的树状结构组织，树中的每个节点就称为一个 znode。你可以把它理解成 zk 中的「文件」或「目录」，比如：`/`、`/app`、`/app/config`、`/services/service1`。

1. 每个 znode 可以存储数据：与传统的文件系统类似，但 znode 存储的数据大小通常不能超过 1MB（通常用于存储小型配置，而不是大块数据）。
2. 每个 znode 可以有子节点：它类似于文件夹，可以嵌套其他 znode，构成树状结构。
3. 临时节点（Ephemeral）会在客户端会话结束时被 zk 自动删除，持久节点（Persistent）创建后会一直存在，直到手动删除，一个节点要么是临时节点要么是持久节点。
4. 顺序节点（Sequential）会自动在节点名后追加一个单调递增的数字，用于实现分布式锁、队列等场景。

### watcher 机制

可以对某个 znode 设置观察者 watcher，一旦该节点发生变化（比如节点创建、内容变更、删除、子节点变化），客户端会收到通知。

### kafka 对 zookeeper 的应用

kafka 在 zk 上使用 `/controller` 临时节点来表示当前集群哪个 broker 是 controller。下面这段代码展示的是一个双 broker 集群上 zk 的`/controller` 节点的信息：

```
{"version":1,"brokerid":0,"timestamp":"1585098432431"}
cZxid = 0x1a
ctime = Wed Mar 25 09:07:12 CST 2020
mZxid = 0x1a
mtime = Wed Mar 25 09:07:12 CST 2020
pZxid = 0x1a
cversion = 0
dataVersion = 0
aclVersion = 0
ephemeralOwner = 0x100002d3a1f0000
dataLength = 54
numChildren = 0
```

其中有两个需要注意的地方：

- brokerid 是 0，表示序号为 0 的 broker 是集群 controller。
- ephemeralOwner 字段不是0x0，说明这是一个临时节点

kafka 利用 zk 提供的 watcher 机制，每个 broker 都会监听 `/controller` 节点的变化随时准备成为 controller 角色，比如 `controller` 节点被删除，即当前集群没有 controller 了，各个 broker 就会去抢着当集群新的 controller。

总而言之，controller 的选举不需要 broker 之间相互通信，而是通过观测 zk 上的节点情况来做决定。

除了 `/controller` 节点外，还有存储其它元数据的节点，不再一一列举，下面遇到再说。

## KafkaController 的字段

KafkaController 代表了 kafka 的 controller 实体，继承了 ControllerEventProcessor，负责各种事件的处理。下面列出了 KafkaController 一些较为重要的字段，看看就行，后面用到的话还会说，列出来这些字段只是想侧面说明 controller 负责干很多与“状态转换”有关的事情，是整个 kafka 集群的心脏。

``` scala
class KafkaController(
  val config: KafkaConfig,
  // zk客户端
  zkClient: KafkaZkClient,
  // 初始broker的信息
  initialBrokerInfo: BrokerInfo,
  // 初始broker epoch
  initialBrokerEpoch: Long,
  // ...
) extends ControllerEventProcessor with Logging with KafkaMetricsGroup {
  
  // controller元数据
  val controllerContext = new ControllerContext
  
  // controller到目标broker的请求发送类
  var controllerChannelManager = new ControllerChannelManager(controllerContext, config, time, metrics,
    stateChangeLogger, threadNamePrefix)

  // 负责定期负载均衡分区leader
  private[controller] val kafkaScheduler = new KafkaScheduler(1)

  // 事件管理器
  private[controller] val eventManager = new ControllerEventManager(config.brokerId, this, time,
    controllerContext.stats.rateAndTimeMetrics)

  // 负责副本状态转换
  val replicaStateMachine: ReplicaStateMachine = new ZkReplicaStateMachine(config, stateChangeLogger, controllerContext, zkClient,
    new ControllerBrokerRequestBatch(config, controllerChannelManager, eventManager, controllerContext, stateChangeLogger))
  
  // 负责分区状态转换
  val partitionStateMachine: PartitionStateMachine = new ZkPartitionStateMachine(config, stateChangeLogger, controllerContext, zkClient,
    new ControllerBrokerRequestBatch(config, controllerChannelManager, eventManager, controllerContext, stateChangeLogger))
  
  // 负责删除主题和日志
  val topicDeletionManager = new TopicDeletionManager(config, controllerContext, replicaStateMachine,
    partitionStateMachine, new ControllerDeletionClient(this, zkClient))
  
  // ...
}
```

除了这些字段外，还有一类字段单独拎出来介绍一下，那就是 zk 的各种监听器，借助这些监听器，controller 便能以观察者的方式轻松应对集群的各种状态变更处理。我将它们监听的东西及其主要功能都写在了字段的注释中：

``` scala
// 监听controller节点的创建/变更/删除
// 处理控制器选举和切换，确保集群中只有一个活跃的控制器
private val controllerChangeHandler = new ControllerChangeHandler(eventManager)
// 监听broker数量变更
// 更新控制器中的Broker列表
private val brokerChangeHandler = new BrokerChangeHandler(eventManager)
// 监听broker信息变更
// 当broker的配置或状态发生变化时，通知控制器更新相关信息
private val brokerModificationsHandlers: mutable.Map[Int, BrokerModificationsHandler] = mutable.Map.empty
// 监听topic数量变更
// 更新控制器中的主题列表和分区分配信息
private val topicChangeHandler = new TopicChangeHandler(eventManager)
// 监听topic删除
// 启动topic删除流程
private val topicDeletionHandler = new TopicDeletionHandler(eventManager)
// 监听partition变更
// 处理partition数量变更、副本分配变更等操作
private val partitionModificationsHandlers: mutable.Map[String, PartitionModificationsHandler] = mutable.Map.empty
// 监听partition重分配请求
// 重新分配分区的副本到不同的Broker上
private val partitionReassignmentHandler = new PartitionReassignmentHandler(eventManager)
// 监听首选副本的选举
// 将分区leader恢复到首选副本，目的是尽可能保障broker间负载均衡
private val preferredReplicaElectionHandler = new PreferredReplicaElectionHandler(eventManager)
// 监听ISR变更
// 更新元数据并通知相关broker
private val isrChangeNotificationHandler = new IsrChangeNotificationHandler(eventManager)
// 监听日志路径变更
// 处理broker日志目录失败事件，如磁盘故障等，触发相应的副本状态变更
private val logDirEventNotificationHandler = new LogDirEventNotificationHandler(eventManager)
```

**需要注意的是，有些监听器是上任 controller 后才会注册，一旦卸任就会注销，比如 brokerChangeHandler，表示只有 controller 才能处理接收到的事件。但是为了防止非 controler 由于分布式竞态问题，也去处理这些事件，在大多数 processXXX 方法中都加一个 isActive 判断自己是不是 controller，不是的话就直接返回**

## controller 选举

接下来，我以 controller 的选举流程为例，引出 KafkaController 的一些方法的实现原理。选举与 ContorollerChangeHandler 监听器息息相关，这个监听器会触发选举流程：

``` scala
class ControllerChangeHandler(eventManager: ControllerEventManager) extends ZNodeChangeHandler {
  // 节点路径字符串，即"/controller"
  override val path: String = ControllerZNode.path

  // /controller节点创建
  override def handleCreation(): Unit = eventManager.put(ControllerChange)
  // /controller节点删除
  override def handleDeletion(): Unit = eventManager.put(Reelect)
  // /controller节点数据变更
  override def handleDataChange(): Unit = eventManager.put(ControllerChange)
}
```

可以看到节点创建和变更发送的都是 ControllerChange 事件，而节点删除则是 Reelect 事件：

``` scala
private def processControllerChange(): Unit = {
  maybeResign()
}

private def processReelect(): Unit = {
  maybeResign()
  elect()
}
```

maybeResign 是用于卸任当前 broker 的 controller 身份，因为当前 broker 不一定是 controller，所以是"maybe"。

可以看到，processReelect 的处理多了一个 elect 的调用，意思是要重新选举出新的 controller，本 broker 作为候选人当然要 elect 参与竞选。除了 /controller 节点清空会导致竞选外，在 KafkaController 启动的 startup 方法中也会发起竞选：

``` scala
def startup() = {
  // ...
  eventManager.put(Startup)
  eventManager.start()
}

private def processStartup(): Unit = {
  zkClient.registerZNodeChangeHandlerAndCheckExistence(controllerChangeHandler)
  // 发起竞选
  elect()
}
```

ok，下面先看看卸任的逻辑：

``` scala
// 根据上次从zk读取的controllerId，判断当前broker是否controller
def isActive: Boolean = activeControllerId == config.brokerId

private def maybeResign(): Unit = {
  val wasActiveBeforeChange = isActive
  zkClient.registerZNodeChangeHandlerAndCheckExistence(controllerChangeHandler)
  activeControllerId = zkClient.getControllerId.getOrElse(-1)
  // 如果之前是controller，但现在不是了，就执行卸任逻辑
  if (wasActiveBeforeChange && !isActive) {
    onControllerResignation()
  }
}

private def onControllerResignation(): Unit = {
  debug("Resigning")
  // 注销一些zk监听器
  zkClient.unregisterZNodeChildChangeHandler(isrChangeNotificationHandler.path)
  zkClient.unregisterZNodeChangeHandler(partitionReassignmentHandler.path)
  zkClient.unregisterZNodeChangeHandler(preferredReplicaElectionHandler.path)
  zkClient.unregisterZNodeChildChangeHandler(logDirEventNotificationHandler.path)
  unregisterBrokerModificationsHandler(brokerModificationsHandlers.keySet)

  // 关闭负责定期重新平衡分区leader的调度器
  kafkaScheduler.shutdown()
  // 重置统计字段
  offlinePartitionCount = 0
  preferredReplicaImbalanceCount = 0
  globalTopicCount = 0
  globalPartitionCount = 0
  topicsToDeleteCount = 0
  replicasToDeleteCount = 0
  ineligibleTopicsToDeleteCount = 0
  ineligibleReplicasToDeleteCount = 0

  // 关闭负责定期清理过期委托令牌的调度器
  if (tokenCleanScheduler.isStarted)
    tokenCleanScheduler.shutdown()

  // 注销zk监听器
  unregisterPartitionReassignmentIsrChangeHandlers()
  // 关闭分区状态机
  partitionStateMachine.shutdown()
  // 注销zk监听器
  zkClient.unregisterZNodeChildChangeHandler(topicChangeHandler.path)
  unregisterPartitionModificationsHandlers(partitionModificationsHandlers.keys.toSeq)
  zkClient.unregisterZNodeChildChangeHandler(topicDeletionHandler.path)
  // shutdown replica state machine
  // 关闭副本状态机
  replicaStateMachine.shutdown()
  // 注销zk监听器
  zkClient.unregisterZNodeChildChangeHandler(brokerChangeHandler.path)

  // 关闭与broker间的所有连接
  controllerChannelManager.shutdown()
  // 清空元数据
  controllerContext.resetContext()

  info("Resigned")
}
```

可以看到，卸任的条件为该 broker 之前是 controller 而现在不是。所谓卸任就是做了一些清理工作，包括注销监听器、重置统计字段、关闭相关组件。

继续看如何竞选，我们重点关注的是如何保证只有一个 broker 可以竞选成功，避免出现脑裂的情况（出现多个 controller）：

``` scala
private def elect(): Unit = {
  activeControllerId = zkClient.getControllerId.getOrElse(-1)
  // 如果当前已经选出了controller（/controller节点不为空），就不再参与竞选
  if (activeControllerId != -1) {
    debug(s"Broker $activeControllerId has been elected as the controller, so stopping the election process.")
    return
  }

  try {
    // 尝试注册为controller
    // 如果注册失败（比如其它broker已经抢先注册），则会抛出ControllerMovedException异常
    val (epoch, epochZkVersion) = zkClient.registerControllerAndIncrementControllerEpoch(config.brokerId)
    // 更新epoch
    controllerContext.epoch = epoch
    controllerContext.epochZkVersion = epochZkVersion
    // 更新activeControllerId为当前brokerId
    activeControllerId = config.brokerId

    // 执行上任后的一些操作
    onControllerFailover()
  } catch {
    case e: ControllerMovedException =>
      // 已经有controller，本broker卸任
      maybeResign()

    case t: Throwable =>
      // 遇到未知异常，卸任当前controller角色，重新选一个controller
      triggerControllerMove()
  }
}
```

所以选举就是两步：

1. 将自身注册到 zk 上，包括更新 controller epoch
2. 执行上任后的一些操作（onControllerFailover）

我们再看看 onControllerFailover 做了什么：

``` scala
private def onControllerFailover(): Unit = {
  info("Registering handlers")

  // 注册监听器
  val childChangeHandlers = Seq(brokerChangeHandler, topicChangeHandler, topicDeletionHandler, logDirEventNotificationHandler,
    isrChangeNotificationHandler)
  childChangeHandlers.foreach(zkClient.registerZNodeChildChangeHandler)
  val nodeChangeHandlers = Seq(preferredReplicaElectionHandler, partitionReassignmentHandler)
  nodeChangeHandlers.foreach(zkClient.registerZNodeChangeHandlerAndCheckExistence)

  // 清理之前controller遗留的日志目录事件、ISR变更事件
  zkClient.deleteLogDirEventNotifications(controllerContext.epochZkVersion)
  zkClient.deleteIsrChangeNotifications(controllerContext.epochZkVersion)
  // 初始化元数据
  initializeControllerContext()
  val (topicsToBeDeleted, topicsIneligibleForDeletion) = fetchTopicDeletionsInProgress()
  // TopicDeletionManager负责管理主题删除的整个生命周期，包括排队、执行、失败重试等
  // 新控制器上任时，需要用最新的主题删除状态初始化它
  topicDeletionManager.init(topicsToBeDeleted, topicsIneligibleForDeletion)

  // 向broker集群广播最新的元数据
  sendUpdateMetadataRequest(controllerContext.liveOrShuttingDownBrokerIds.toSeq, Set.empty)

  // 启动副本状态机和分区状态机
  replicaStateMachine.startup()
  partitionStateMachine.startup()

  // 此时，本broker作为controller已经准备好

  // 继续分区重分配、主题删除、首选分区副本的选举
  initializePartitionReassignments()
  topicDeletionManager.tryTopicDeletion()
  val pendingPreferredReplicaElections = fetchPendingPreferredReplicaElections()
  onReplicaElection(pendingPreferredReplicaElections, ElectionType.PREFERRED, ZkTriggered)
  
  // 启动scheudler（用于定期负载均衡分区leader）
  kafkaScheduler.startup()
  if (config.autoLeaderRebalanceEnable) {
    scheduleAutoLeaderRebalanceTask(delay = 5, unit = TimeUnit.SECONDS)
  }

  // 启动tokenCleanScheduler（用于定期清理过期的委托令牌）
  if (config.tokenAuthEnabled) {
    info("starting the token expiry check scheduler")
    tokenCleanScheduler.startup()
    tokenCleanScheduler.schedule(name = "delete-expired-tokens",
      fun = () => tokenManager.expireTokens,
      period = config.delegationTokenExpiryCheckIntervalMs,
      unit = TimeUnit.MILLISECONDS)
  }
}
```

可以看到，broker 通过向 zk 注册上任 controller 之后，还要做一堆的事情，都写在了注释里。

## controller 集群成员管理

官方文档说，当我们要扩展集群的大小时，只需要给该 broker 分配唯一 brokerId，然后启动 kafka 服务端程序就 OK 了：

> Adding servers to a Kafka cluster is easy, just assign them a unique broker id and start up Kafka on your new servers.

其实这就是 controller 对集群的管理功能起作用了。controller 对集群的管理包括对集群成员的加入与退出、对单个成员的数据改变。对于上面说的扩展集群原理如下：

每个 broker 在启动的时候，会在 zk 的 `/brokers/ids` 节点下创建一个名为 `broker.id` 参数值的**临时节点**。比如 broker 的 broker.id 参数值设置为 1001，那么，当 broker 启动后，你会在 zk 的 `/brokers/ids` 下观测到一个名为 1001 的子节点，该节点的内容包括了 broker 配置的主机名、端口号以及所用监听器的信息。当 broker 关闭或与 zk 意外断连，该节点又会自动删除。

因此，结合 zk 的监听器，controller 就能监听 `/brokers/ids` 下的 broker 数量变化：

``` scala
class BrokerChangeHandler(eventManager: ControllerEventManager) extends ZNodeChildChangeHandler {
  override val path: String = BrokerIdsZNode.path

  override def handleChildChange(): Unit = {
    eventManager.put(BrokerChange)
  }
}
```

controller 对 BrokerChange 的处理如下：

``` scala
private def processBrokerChange(): Unit = {
  if (!isActive) return
  // 从zk获取最新的broker元数据，从controllerContex获取本地缓存的broker元数据
  // 通过将两者比对，得知：
  // 1. 当前存活的所有broker
  // 2. 加入集群的broker
  // 3. 退出集群的broker
  // 4. 重启过的broker
  val curBrokerAndEpochs = zkClient.getAllBrokerAndEpochsInCluster
  val curBrokerIdAndEpochs = curBrokerAndEpochs map { case (broker, epoch) => (broker.id, epoch) }
  val curBrokerIds = curBrokerIdAndEpochs.keySet
  val liveOrShuttingDownBrokerIds = controllerContext.liveOrShuttingDownBrokerIds
  val newBrokerIds = curBrokerIds -- liveOrShuttingDownBrokerIds
  val deadBrokerIds = liveOrShuttingDownBrokerIds -- curBrokerIds
  val bouncedBrokerIds = (curBrokerIds & liveOrShuttingDownBrokerIds)
    .filter(brokerId => curBrokerIdAndEpochs(brokerId) > controllerContext.liveBrokerIdAndEpochs(brokerId))
  val newBrokerAndEpochs = curBrokerAndEpochs.filter { case (broker, _) => newBrokerIds.contains(broker.id) }
  val bouncedBrokerAndEpochs = curBrokerAndEpochs.filter { case (broker, _) => bouncedBrokerIds.contains(broker.id) }
  val newBrokerIdsSorted = newBrokerIds.toSeq.sorted
  val deadBrokerIdsSorted = deadBrokerIds.toSeq.sorted
  val liveBrokerIdsSorted = curBrokerIds.toSeq.sorted
  val bouncedBrokerIdsSorted = bouncedBrokerIds.toSeq.sorted

  // 连接新broker
  newBrokerAndEpochs.keySet.foreach(controllerChannelManager.addBroker)
  // 重新连接重启的broker
  bouncedBrokerIds.foreach(controllerChannelManager.removeBroker)
  bouncedBrokerAndEpochs.keySet.foreach(controllerChannelManager.addBroker)
  // 断开已退出的broker
  deadBrokerIds.foreach(controllerChannelManager.removeBroker)
  
  // 记录新broker到缓存的元数据中，回调onBrokerStartup
  if (newBrokerIds.nonEmpty) {
    controllerContext.addLiveBrokersAndEpochs(newBrokerAndEpochs)
    onBrokerStartup(newBrokerIdsSorted)
  }
  // 重新记录重启的broker到缓存的元数据中，回调onBrokerFailure与onBrokerStartup
  if (bouncedBrokerIds.nonEmpty) {
    controllerContext.removeLiveBrokers(bouncedBrokerIds)
    onBrokerFailure(bouncedBrokerIdsSorted)
    controllerContext.addLiveBrokersAndEpochs(bouncedBrokerAndEpochs)
    onBrokerStartup(bouncedBrokerIdsSorted)
  }
  // 从缓存的元数据中移除旧broker，回调onBrokerFailure
  if (deadBrokerIds.nonEmpty) {
    controllerContext.removeLiveBrokers(deadBrokerIds)
    onBrokerFailure(deadBrokerIdsSorted)
  }
}
```

也就是说，当 broker 集群成员发生变动，controller 将会：

1. 建立/断开与 broker 的连接
2. 更新本地元数据缓存
3. 回调 onBrokerFailure/onBrokerStartup

下面继续追踪回调函数干了什么：

``` scala
// onBrokerStartup主要干了三件事情：
// 1. 通知broker更新元数据
// 2. 通知分区和副本状态机有副本/分区上线，触发状态变更
// 3. 恢复分区重分配以及主题删除流程
private def onBrokerStartup(newBrokers: Seq[Int]): Unit = {
  // 新broker不应该有离线目录，清除这些离线目录
  newBrokers.foreach(controllerContext.replicasOnOfflineDirs.remove)
  val newBrokersSet = newBrokers.toSet
  val existingBrokers = controllerContext.liveOrShuttingDownBrokerIds -- newBrokers
  // 通知已有的broker更新broker列表
  sendUpdateMetadataRequest(existingBrokers.toSeq, Set.empty)
  // 通知新broker更新broker列表，以及所有主题分区信息
  sendUpdateMetadataRequest(newBrokers, controllerContext.partitionLeadershipInfo.keySet)
  // 通知副本状态机这些新broker上的分区处于在线状态，这会启动这些副本的高水位线线程
  val allReplicasOnNewBrokers = controllerContext.replicasOnBrokers(newBrokersSet)
  replicaStateMachine.handleStateChanges(allReplicasOnNewBrokers.toSeq, OnlineReplica)
  // 触发分区状态机，尝试将处于新建或离线状态的分区转为在线状态。这可能会导致新broker成为某些分区的leader
  partitionStateMachine.triggerOnlinePartitionStateChange()
  // 恢复分区重分配
  maybeResumeReassignments { (_, assignment) =>
    assignment.targetReplicas.exists(newBrokersSet.contains)
  }
  // 恢复主题删除
  val replicasForTopicsToBeDeleted = allReplicasOnNewBrokers.filter(p => topicDeletionManager.isTopicQueuedUpForDeletion(p.topic))
  if (replicasForTopicsToBeDeleted.nonEmpty) {
    topicDeletionManager.resumeDeletionForTopics(replicasForTopicsToBeDeleted.map(_.topic))
  }
  // 为新broker注册BrokerModifications监听器
  registerBrokerModificationsHandler(newBrokers)
}

private def onBrokerFailure(deadBrokers: Seq[Int]): Unit = {
  // 不再记录下线broker的离线目录信息
  deadBrokers.foreach(controllerContext.replicasOnOfflineDirs.remove)
  val deadBrokersThatWereShuttingDown =
    deadBrokers.filter(id => controllerContext.shuttingDownBrokerIds.remove(id))
  if (deadBrokersThatWereShuttingDown.nonEmpty)
  val allReplicasOnDeadBrokers = controllerContext.replicasOnBrokers(deadBrokers.toSet)
  // 处理受控关闭并最终下线的broker上的分区副本
  onReplicasBecomeOffline(allReplicasOnDeadBrokers)
	// 注销下线broker的BrokerModifications监听器
  unregisterBrokerModificationsHandler(deadBrokers)
}
```

我们继续跟踪对那些离线副本的处理：

``` scala
private def onReplicasBecomeOffline(newOfflineReplicas: Set[PartitionAndReplica]): Unit = {
  // 离线副本分为两类，处理方式不同
  // 1. 属于待删除主题
  // 2. 属于正常主题
  val (newOfflineReplicasForDeletion, newOfflineReplicasNotForDeletion) =
    newOfflineReplicas.partition(p => topicDeletionManager.isTopicQueuedUpForDeletion(p.topic))

  // 找出当前「leader所在broker已经下线并且其所属主题不是待删除」的分区，然后：
  // 1. 将这些分区标记为离线状态（OfflinePartition），表示当前分区没有可用leader
  // 2. 触发分区状态机，尝试将这些离线分区转为在线状态，实际上就是为这些分区选举新leader以恢复可用性
  val partitionsWithoutLeader = controllerContext.partitionLeadershipInfo.filter(partitionAndLeader =>
    !controllerContext.isReplicaOnline(partitionAndLeader._2.leaderAndIsr.leader, partitionAndLeader._1) &&
      !topicDeletionManager.isTopicQueuedUpForDeletion(partitionAndLeader._1.topic)).keySet
  partitionStateMachine.handleStateChanges(partitionsWithoutLeader.toSeq, OfflinePartition)
  partitionStateMachine.triggerOnlinePartitionStateChange()
  
  // 将属于正常主题的副本从ISR中移除，并将它们状态变更为OfflineReplica
  replicaStateMachine.handleStateChanges(newOfflineReplicasNotForDeletion.toSeq, OfflineReplica)

  // 将属于待删除主题的副本标记为删除失败状态，避免无限重试删除
  if (newOfflineReplicasForDeletion.nonEmpty) {
    topicDeletionManager.failReplicaDeletion(newOfflineReplicasForDeletion)
  }

  // 如果没有分区需要进行leader重新选举，更新其它broker的元数据（更新broker列表）
  // 否则不用发，因为在leader选举过程中已经发送了元数据更新
  if (partitionsWithoutLeader.isEmpty) {
    sendUpdateMetadataRequest(controllerContext.liveOrShuttingDownBrokerIds.toSeq, Set.empty)
  }
}
```

上面介绍的是 broker 的增删，而下面来说说 broker 本身内容变更的处理。细心的读者会注意到，当 broker 新增的时候会注册 BrokerModificationsHandler 这个监听器，下线的时候又会给注销掉。没错，这个监听器就是用来监听 broker 自身信息的变更：

``` scala
class BrokerModificationsHandler(eventManager: ControllerEventManager, brokerId: Int) extends ZNodeChangeHandler {
  override val path: String = BrokerIdZNode.path(brokerId)

  override def handleDataChange(): Unit = {
    eventManager.put(BrokerModifications(brokerId))
  }
}
```

本来 ZNodeChangeHandler 中有三种事件可以处理，分别是节点创建、删除以及变更。但 BrokerModificationsHandler 只关心节点内容变更（因为创建和删除事件其实就是 broker 的增删，在 BrokerChangeHandler 中已经统一进行处理了）。再重申一遍，这里说的节点就是 zk 上 `/broker/ids/{brokerId}` 数据节点。

``` scala
private def processBrokerModification(brokerId: Int): Unit = {
  if (!isActive) return
  // 分别获取新旧broker的连接信息
  val newMetadataOpt = zkClient.getBroker(brokerId)
  val oldMetadataOpt = controllerContext.liveOrShuttingDownBroker(brokerId)
  if (newMetadataOpt.nonEmpty && oldMetadataOpt.nonEmpty) {
    val oldMetadata = oldMetadataOpt.get
    val newMetadata = newMetadataOpt.get
    // 如果连接信息发生变更，则更新元数据，并且回调onBrokerUpdate进行处理
    if (newMetadata.endPoints != oldMetadata.endPoints) {
      controllerContext.updateBrokerMetadata(oldMetadata, newMetadata)
      onBrokerUpdate(brokerId)
    }
  }
}

private def onBrokerUpdate(updatedBrokerId: Int): Unit = {
  // 通知其它broker更新元数据
  sendUpdateMetadataRequest(controllerContext.liveOrShuttingDownBrokerIds.toSeq, Set.empty)
}
```

比较简单，所谓成员信息管理只是更新一下元数据。

> [!warning]
>
> **但是有个问题！这里 broker 的 endpoints 变了，我觉得也应该去更新一下 ControllerChannelManager 中维护的该 broker 的连接信息，否则 ControllerChannelManager 不就会一直重试往旧 endpoints 发消息了吗？进一步地说，如果真存在这个设计缺陷，是不是就得重启 controller 来解决了。蹲个好心人解答一下**

## controller 主题管理

除了维护集群成员之外，controller 还有一个重要的任务，那就是对所有主题进行管理，主要包括主题的创建、变更与删除。

一共涉及到两个监听器：

``` scala
// 负责主题的创建和变更
class TopicChangeHandler(eventManager: ControllerEventManager) extends ZNodeChildChangeHandler {
  // 节点/brokers/topics
  override val path: String = TopicsZNode.path

  override def handleChildChange(): Unit = eventManager.put(TopicChange)
}

// 负责主题的删除
class TopicDeletionHandler(eventManager: ControllerEventManager) extends ZNodeChildChangeHandler {
  // 节点/admin/delete_topics
  override val path: String = DeleteTopicsZNode.path

  override def handleChildChange(): Unit = eventManager.put(TopicDeletion)
}
```

先来看下主题的创建和变更：

``` scala
private def processTopicChange(): Unit = {
  if (!isActive) return
  val topics = zkClient.getAllTopicsInCluster
  // 新增的topic
  val newTopics = topics -- controllerContext.allTopics
  // 删除的topic
  val deletedTopics = controllerContext.allTopics -- topics
  controllerContext.allTopics = topics

  // 为新topic注册PartitionModifications监听器
  registerPartitionModificationsHandlers(newTopics.toSeq)
  val addedPartitionReplicaAssignment = zkClient.getFullReplicaAssignmentForTopics(newTopics)
  deletedTopics.foreach(controllerContext.removeTopic)
  // 更新缓存元数据
  addedPartitionReplicaAssignment.foreach {
    case (topicAndPartition, newReplicaAssignment) => controllerContext.updatePartitionFullReplicaAssignment(topicAndPartition, newReplicaAssignment)
  }
  // 如果有新增的副本，回调onNewPartitionCreation
  if (addedPartitionReplicaAssignment.nonEmpty)
    onNewPartitionCreation(addedPartitionReplicaAssignment.keySet)
}
```

继续看看 onNewPartitionCreation：

``` scala
private def onNewPartitionCreation(newPartitions: Set[TopicPartition]): Unit = {
  // 通知分区状态机有新分区加入
  partitionStateMachine.handleStateChanges(newPartitions.toSeq, NewPartition)
  // 通知副本状态机有新副本加入
  replicaStateMachine.handleStateChanges(controllerContext.replicasForPartition(newPartitions).toSeq, NewReplica)
  // 通知分区状态机让新分区上线，并选举leader
  partitionStateMachine.handleStateChanges(
    newPartitions.toSeq,
    OnlinePartition,
    Some(OfflinePartitionLeaderElectionStrategy(false))
  )
  // 通知副本状态机让新副本上线
  replicaStateMachine.handleStateChanges(controllerContext.replicasForPartition(newPartitions).toSeq, OnlineReplica)
}
```

可以看到，TopicChange 主要是处理 topic 数量的变更，具体包括更新元数据以及通知分区/副本状态机。

**状态机这个东西之前也出现了好几次，目前只需要知道，当分区和副本只要处于“上线”状态就能正常工作即可。**

下面再看看 topic 删除的处理：

``` scala
private def processTopicDeletion(): Unit = {
  if (!isActive) return
  // 获取被删除的topic
  var topicsToBeDeleted = zkClient.getTopicDeletions.toSet
  // 忽略不存在的topic
  val nonExistentTopics = topicsToBeDeleted -- controllerContext.allTopics
  if (nonExistentTopics.nonEmpty) {
    zkClient.deleteTopicDeletions(nonExistentTopics.toSeq, controllerContext.epochZkVersion)
  }
  topicsToBeDeleted --= nonExistentTopics
  if (config.deleteTopicEnable) {
    if (topicsToBeDeleted.nonEmpty) {
      // 遍历并删除topic
      topicsToBeDeleted.foreach { topic =>
        // 将正在重分配的topic标记为"ineligible for deletion"
        val partitionReassignmentInProgress =
          controllerContext.partitionsBeingReassigned.map(_.topic).contains(topic)
        if (partitionReassignmentInProgress)
          topicDeletionManager.markTopicIneligibleForDeletion(Set(topic),
            reason = "topic reassignment in progress")
      }
      // 将待删除topic入队，交由TopicDeletionManager处理
      topicDeletionManager.enqueueTopicsForDeletion(topicsToBeDeleted)
    }
  } else {
    // 用户已经配置了禁止删除topic
    info(s"Removing $topicsToBeDeleted since delete topic is disabled")
    zkClient.deleteTopicDeletions(topicsToBeDeleted.toSeq, controllerContext.epochZkVersion)
  }
}
```

## 总结

总的来说，controller 利用了 zk 的观察者模式，通过设置节点监听器，来观测集群中各种事件的发生，然后将他们抽象成一个个的事件（ControllerEvent），放到事件队列中最终单线程地一个个去处理。

在 controller 工作过程中（这里特指 KafkaController 这个类的工作过程中），用到了很多的组件，比如之前介绍到的 ControllerChannelManager、ControllerContext、ControllerEventManager，这些组件必须与 controller 组件紧耦合在一起才能实现各自的功能。

但 controller 也使用到了一些功能相对独立的组件，比如 TopicDeletionManager、ReplicaStateMachine、PartitionStateMachine，后续将会一一深入分析。

## 参考

极客时间《Kafka核心源码解读》——胡夕
