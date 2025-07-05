---
date: 2025-07-04T11:10:27+08:00
title: Kafka源码阅读（12）-Controller之单线程事件处理器
categories: [源码阅读,kafka]
tags: [kafka,mq,源码]
draft: false
---

> [!note]
>
> 基于开源 kafka 2.5 版本。
>
> 如无特殊说明，文中代码片段将删除 debug 信息、异常触发、英文注释等代码，以便观看核心代码。

读者要注意，本篇要介绍的单线程事件处理器与上一篇的内容并没有非常强的关联，因为我们是以“自下而上”的方式进行描述。你只需要知道，上篇的 ControllerChannelManager 只是作为本篇单线程事件处理器中的某个事件的处理要用到的组件而已。所以不必纠结内容的跳跃性，只需要先大概看懂每块内容，我们最终就能理解并拼出完整的 kafka 架构图。

在 0.11.0.0 版本之前，Controller 组件的源码非常复杂。集群元数据信息在程序中同时被多个线程访问，因此，源码里有大量的 Monitor 锁、Lock 锁或其他线程安全机制，这就导致，这部分代码读起来晦涩难懂，改动起来也困难重重。鉴于这个原因，自 0.11.0.0 版本开始，社区陆续对 Controller 代码结构进行了改造。其中非常重要的一环，就是将**多线程并发访问的方式改为了单线程的事件队列方式**。

这里的单线程，并非是指 Controller 只有一个线程了，而是指**对局部状态的访问限制在一个专属线程上**，即让这个特定线程排他性地操作 Controller 元数据信息。用图简单表示就是：

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20250704202645336.png" alt="image-20250704202645336" style="zoom:50%;" />

其实也是之前经常出现的“生产者-消费者”模型而已，这个模型确实好用。结合上面这张图，参与事件处理的核心类有四个（不包括前面那些杂七杂八的“生产者”线程），分别是：

1. 事件类 ControllerEvent：没什么好说，就是在事件队列中所谓的“事件”
2. 事件轮询线程 ControllerEventThread：也叫事件处理线程，最终调用 ControllerEventProcessor.process 来处理事件
3. 事件管理器 ControllerEventManager：封装了事件队列以及事件处理线程，其他线程只需要调用 put 方法发送事件
4. 事件处理器接口 ControllerEventProcessor：用于真正处理事件。只有 KafkaController 一个实现类

其实看懂了这张图的话，「单线程事件处理器」也就掌握了一半了，剩下的就是一些扩展内容以及源码。

## 事件 ControllerEvent

ControllerEvent 本质上是一个 trait：

``` scala
sealed trait ControllerEvent {
  def state: ControllerState
}
```

不同类型的事件都继承这个 trait，举几个事件类型的例子：

``` scala
// 注册broker并重新选举事件
case object RegisterBrokerAndReelect extends ControllerEvent {
  override def state: ControllerState = ControllerState.ControllerChange
}

// 重新选举事件
case object Reelect extends ControllerEvent {
  override def state = ControllerState.ControllerChange
}

// 关闭事件处理线程事件
case object ShutdownEventThread extends ControllerEvent {
  def state = ControllerState.ControllerShutdown
}
```

注意到 ControllerEvent 中还有一个 ControllerState 类型的字段，我们在下一小节会说。

首先，在 KafkaControll 类中的 state 方法用于表示 controller 当前状态：

``` scala
private def state: ControllerState = eventManager.state
```

这个方法返回的是 ControllerEventManager 中的同名方法，该方法返回内部的 _state 字段：

``` scala
@volatile private var _state: ControllerState = ControllerState.Idle
def state: ControllerState = _state
```

因此 ControllerEventManager._state 就表示 controller 当前的状态。关于这个 _state 与 ControllerEvent.state 有什么关系，我们在下一小节会分析。

另外，事件队列并不直接存储 ControllerEvent，而是将其包装在 QueuedEvent 中，主要是为了包装「避免重复处理」的逻辑：

``` scala
class QueuedEvent(val event: ControllerEvent,
                  val enqueueTimeMs: Long) {
  val processingStarted = new CountDownLatch(1)
  // 记录本事件是否被处理过
  val spent = new AtomicBoolean(false)

  // 处理
  def process(processor: ControllerEventProcessor): Unit = {
    // 避免本事件重复处理
    if (spent.getAndSet(true))
      return
    processingStarted.countDown()
    processor.process(event)
  }

  // 抢占处理
  def preempt(processor: ControllerEventProcessor): Unit = {
    // 避免本事件重复处理
    if (spent.getAndSet(true))
      return
    processor.preempt(event)
  }

  // 等待事件被处理完毕
  def awaitProcessing(): Unit = {
    processingStarted.await()
  }
}
```

## 事件轮询线程 ControllerEventThread

事件轮询线程的代码很短：

``` scala
class ControllerEventThread(name: String) extends ShutdownableThread(name = name, isInterruptible = false) {
  logIdent = s"[ControllerEventThread controllerId=$controllerId] "

  override def doWork(): Unit = {
    val dequeued = queue.take()
    dequeued.event match {
      case ShutdownEventThread => // The shutting down of the thread has been initiated at this point. Ignore this event.
      case controllerEvent =>
        _state = controllerEvent.state

        eventQueueTimeHist.update(time.milliseconds() - dequeued.enqueueTimeMs)

        try {
          def process(): Unit = dequeued.process(processor)

          rateAndTimeMetrics.get(state) match {
            case Some(timer) => timer.time { process() }
            case None => process()
          }
        } catch {
          case e: Throwable => error(s"Uncaught error processing event $controllerEvent", e)
        }

        _state = ControllerState.Idle
    }
  }
}
```

其实它做的事情很简单，就是不断从队列取出事件交给 ControllerEventProcessor 去处理。我把 doWork 再简化一下：

``` scala
override def doWork(): Unit = {
  val dequeued = queue.take()
  dequeued.event match {
    case controllerEvent =>
      // 状态变更成事件的状态
      _state = controllerEvent.state

      // 处理事件
      dequeued.process(processor)

      // 状态变更成Idle（空闲）
      _state = ControllerState.Idle
  }
}
```

这里我们就知道了 controller 接收到事件时 controller 设置为该事件对应的状态。不同的事件可能属于同一种状态，比如 RegisterBrokerAndReelect 事件和 Reelect 事件都属于 ControllerChange 状态。

至于为什么要记录 controller 的状态，其实是为了监控 controller 状态的变更速率，比如监控到某些状态变更速率异常的时候，进一步确定可能造成瓶颈的 controller 事件，并调试问题。

## 事件管理器 ControllerEventManager

叫是叫“管理器”，但看图就知道没这么复杂，不过就是把事件队列和那个单线程封装一下，对外提供一个 put 方法来发送事件而已。

另外我们知道 QueuedEvent 有 process 和 preemt 方法。process 好理解，就是常规的调用路径：

```
ControllerEventManager.put
ControllerEventThread从队列取出事件
QueuedEvent.process
ControllerEventProcessor.process
```

而 preempt 不是这个调用路径，而是：

```
ControllerEventManager.clearAndPut
QueuedEvent.preempt
ControllerEventProcessor.preempt
```

实际上，clearAndPut 就是为了清空当前队列的事件，然后处理 put 进去的那个事件。比如：

- 在 zookeeper 会话过期时，在开启新的会话前，会调用 beforeInitializingSession方法，其中使用 clearAndPut(Expire) 确保了所有待处理事件在创建新会话前被处理
- 在 KafkaController 的 shutdown 方法中，使用 clearAndPut(ShutdownEventThread) 确保关闭事件被立即处理

另外我们发现 clearAndPut 不是简单地清空队列，而是还会调用被清空事件的 preempt 方法，因为某些事件包含回调函数，即使事件被抢占，也需要通知调用者（实际上只有以下两种事件需要在被清理前调用回调函数）：

``` scala
// KafkaController
override def preempt(event: ControllerEvent): Unit = {
  event match {
    case ReplicaLeaderElection(partitions, _, _, callback) =>
      preemptReplicaLeaderElection(partitions, callback)
    case ControlledShutdown(id, brokerEpoch, callback) =>
      preemptControlledShutdown(id, brokerEpoch, callback)
    case _ =>
  }
}
```

## 事件处理器 ControllerEventProcessor

用于最终处理事件的接口，只有 KafkaController 一个实现类。

``` scala
trait ControllerEventProcessor {
  def process(event: ControllerEvent): Unit
  def preempt(event: ControllerEvent): Unit
}
```

其中 preempt 我们上面已经提前看过，而 process 其实也类似：

``` scala
override def process(event: ControllerEvent): Unit = {
  try {
    event match {
      case event: MockEvent =>
        // Used only in test cases
        event.process()
      case ShutdownEventThread =>
        error("Received a ShutdownEventThread event. This type of event is supposed to be handle by ControllerEventThread")
      case AutoPreferredReplicaLeaderElection =>
        processAutoPreferredReplicaLeaderElection()
      case // ...
    }
  } catch {
    case e: ControllerMovedException =>
      info(s"Controller moved to another broker when processing $event.", e)
      maybeResign()
    case e: Throwable =>
      error(s"Error processing event $event", e)
  } finally {
    updateMetrics()
  }
}
```

XXX 事件对应 processXXX 方法进行处理。

## 总结

本篇介绍了 Controller 中的单线程事件处理器模型，该模型旨在大大减低之前 controller 基于多线程处理事件的复杂度，使用单线程更易维护和可读。

这两篇学习了 controller 相关的一些基础组件，包括元数据类、与broker间通信的管理类、单线程事件处理类，在下一篇中将进入真正 controller 的学习，即 KafkaController。

## 参考

极客时间《Kafka核心源码解读》——胡夕
