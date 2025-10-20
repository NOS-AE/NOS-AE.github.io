---
date: 2025-10-04T13:50:47+08:00
title: Informer原理详解
tags: [k8s,源码,informer]
categories: [源码阅读,client-go]
draft: false
---

> [!note]
>
> 基于client-go@v0.31.13

## informer介绍

informer是k8s客户端库提供的一个组件，用于 **资源监听+缓存**，用于高效感知k8s集群中的资源变化。

实际上它就是构建controller的基础。比如我们用kubebuilder搭好一个CRD的脚手架后，我们只需要去实现Reconcile方法进行资源的调谐，不需要关心Reconcile什么时候会被调用。底层其实是informer监听到资源变化后会自动回调Reconcile，即：

- Informer：负责监听和缓存资源变化
- Controller：负责消费这些变化，比如执行 **Reconcile**（调谐逻辑）

当然，informer可以单独拿出来用，它并不与controller强绑定，只是informer+controller是最常见的用法。

## informer架构

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/client-go-controller-interaction.jpeg)

图来自 [client-go under the hood](https://github.com/kubernetes/sample-controller/blob/master/docs/controller-client-go.md)。

上半部分是client-go库内部实现informer的相关组件，下半部分是用户自定义controller

介绍一下图中的核心组件：

- **Reflector**：负责从apiserver获取资源的全量数据（list）并持续监听增量变化（watch）
- **DeltaFIFO**：先进先出的队列，队列元素就是资源对象的历史变更事件
- **Indexer**：存放全量的资源对象，并提供索引用于快速访问对象
- **Informer**：消费队列中的事件，分发给ResourceEventHandler进行处理

以及用户代码部分的组件：

- ResourceEventHandler：用于当事件发生时的处理回调函数，由informer进行调用。该回调函数的实现一般是拿到对象的key然后放进workqueue中等待下一步的处理
- WorkQueue：用于解耦对象（从informer）的接收以及处理
- ProcessItem：用于处理对象的函数，一般会持有Indexer的引用，通过key快速查询对象

## 示例代码

talk is cheap，我们来看下如何使用informer实现一个最简单的，只是简单打印一下资源对象增删改事件的程序，图中大部分组件都涉及。省略了WorkerQueue、ProcessItem等，因为这些不是本文的重点。

``` go
package main

import (
  "context"
  "flag"
  "fmt"
  "path/filepath"
  "time"

  v1 "k8s.io/api/core/v1"
  metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
  "k8s.io/client-go/informers"
  "k8s.io/client-go/kubernetes"
  "k8s.io/client-go/tools/cache"
  "k8s.io/client-go/tools/clientcmd"
)

func main() {
  // 1. 加载 kubeconfig
  kubeconfig := filepath.Join(
    homeDir(), ".kube", "config",
  )
  config, err := clientcmd.BuildConfigFromFlags("", kubeconfig)
  if err != nil {
    panic(err)
  }

  // 2. 创建 clientset，reflector会用它来进行listAndWatch
  clientset, err := kubernetes.NewForConfig(config)
  if err != nil {
    panic(err)
  }

  // 3. 创建工厂对象 SharedInformerFactory，用于创建informer以及统一启动所有informer
  factory := informers.NewSharedInformerFactory(clientset, 30*time.Second)

  // 4. 获取 Pod 的 Informer
  podInformer := factory.Core().V1().Pods().Informer()

  // 5. 注册 ResourceEventHandler
  podInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
    AddFunc: func(obj interface{}) {
      pod := obj.(*v1.Pod)
      fmt.Printf("[ADD] Pod %s/%s\n", pod.Namespace, pod.Name)
    },
    UpdateFunc: func(oldObj, newObj interface{}) {
      oldPod := oldObj.(*v1.Pod)
      newPod := newObj.(*v1.Pod)
      fmt.Printf("[UPDATE] Pod %s/%s -> Phase: %s -> %s\n",
          newPod.Namespace, newPod.Name,
          oldPod.Status.Phase, newPod.Status.Phase)
    },
    DeleteFunc: func(obj interface{}) {
      pod := obj.(*v1.Pod)
      fmt.Printf("[DELETE] Pod %s/%s\n", pod.Namespace, pod.Name)
    },
  })

  // 6. 启动所有通过这个工厂创建的 informer
  stopCh := make(chan struct{})
  factory.Start(stopCh)

  // 7. 等待同步完成
  factory.WaitForCacheSync(stopCh)
  fmt.Println("Pod informer started, listening...")
  
  <-stopCh
}

// 获取 home 目录
func homeDir() string {
  if h := filepath.Clean("~"); h != "" {
    return h
  }
  return "/root"
}

```

## Reflector

Reflector负责保持本地**store（DeltaFIFO）**与API Server中的资源内容同步。

Reflector构造函数如下，创建一个Reflector必须指定一个ListerWatcher以及要监听的资源类型，由此可以看出，一个reflector只监听一种资源类型（比如只监听pod）

``` go
func NewReflectorWithOptions(lw ListerWatcher, expectedType interface{}, store Store, options ReflectorOptions) *Reflector
```

ListerWatcher是将Lister和Watcher两个接口合并成同一个接口（类似ReaderWriter那样），提供List和Watch的能力，具体实现是cache.ListWatch：

``` go
type ListWatch struct {
	ListFunc  ListFunc
	WatchFunc WatchFunc
}

func (lw *ListWatch) List(options metav1.ListOptions) (runtime.Object, error) {
	return lw.ListFunc(options)
}

func (lw *ListWatch) Watch(options metav1.ListOptions) (watch.Interface, error) {
	return lw.WatchFunc(options)
}
```

具体的实现交给了ListFunc和WatchFunc，下面看看pod的list和watch：

``` go
// client就是ClientSet
func NewFilteredPodInformer(client kubernetes.Interface, /* ... */) cache.SharedIndexInformer {
	return cache.NewSharedIndexInformer(
		&cache.ListWatch{
      // List
			ListFunc: func(options metav1.ListOptions) (runtime.Object, error) {
				return client.CoreV1().Pods(namespace).List(context.TODO(), options)
			},
      // Watch
			WatchFunc: func(options metav1.ListOptions) (watch.Interface, error) {
				return client.CoreV1().Pods(namespace).Watch(context.TODO(), options)
			},
		},
		// ...
	)
}
```

由此可以看到，informer其实是对client提供的List和Watch能力的进一步抽象与封装：

Reflector通过Run方法运行，它会调用ListAndWatch处理具体逻辑。传统的ListWatch是通过chunking方式list完成后关闭连接，再开新的watch长连接：

```
List(获取快照) → Watch(监听变化)
```

而现在informer使用同一个watch连接，先发全量数据，继续使用同一条连接获取增量数据：

```
Watch(带 SendInitialEvents) → 收到所有 Added → Bookmark → 继续 Watch
```

相比传统方式，使用同一条连接进行list+watch的好处在于这样可以降低apiserver的压力，详情可见[proposal](https://github.com/kubernetes/enhancements/tree/master/keps/sig-api-machinery/3157-watch-list#proposal)。

``` go
// ListWatch方法会获取全量资源对象以及持续监听后续变更
func (r *Reflector) ListAndWatch(stopCh <-chan struct{}) error {
	klog.V(3).Infof("Listing and watching %v from %s", r.typeDescription, r.name)
	var err error
	var w watch.Interface
	useWatchList := ptr.Deref(r.UseWatchList, false)
	fallbackToList := !useWatchList

  // stream方式list
	if useWatchList {
		w, err = r.watchList(stopCh)
		if err != nil {
			klog.Warningf("The watchlist request ended with an error, falling back to the standard LIST/WATCH semantics because making progress is better than deadlocking, err = %v", err)
			fallbackToList = true
			w = nil
		}
	}

  // chunking方式list（fallback行为）
	if fallbackToList {
		err = r.list(stopCh)
		if err != nil {
			return err
		}
	}

  // watch
	return r.watchWithResync(w, stopCh)
}
```

下面把两种方式都介绍下，做个对比

### 流式list

``` go
func (r *Reflector) watchList(stopCh <-chan struct{}) (watch.Interface, error) {
	// ...
  
  for {
    // ...
    
    // 最后一次观察到的最大RV，该RV作为List和Watch的分界点，即小于该RV的对象属于全量数据，后续的对象都属于增量数据。初始时还没有观察到任何数据，因此RV=""，表示使用当前集群最大RV
		lastKnownRV := r.rewatchResourceVersion()
    // 临时存储接收到的全量
		temporaryStore = NewStore(DeletionHandlingMetaNamespaceKeyFunc)
    
		options := metav1.ListOptions{
			ResourceVersion:      lastKnownRV,
			AllowWatchBookmarks:  true,
      // 为了以watch的方式进行list，使用该参数指定watch在发送增量数据之前还要发送当前的全量数据，并且这些全量数据以Added事件表示
			SendInitialEvents:    pointer.Bool(true),
			ResourceVersionMatch: metav1.ResourceVersionMatchNotOlderThan,
			TimeoutSeconds:       &timeoutSeconds,
		}

    // 开始流式list
		w, err = r.listerWatcher.Watch(options)
    
		// ...
    
    // 处理接收的到的数据，并存入temporaryStore中
		watchListBookmarkReceived, err := handleListWatch(start, w, temporaryStore, r.expectedType, r.expectedGVK, r.name, r.typeDescription,
			func(rv string) { resourceVersion = rv },
			r.clock, make(chan error), stopCh)

    // ...
    
    // 收到了Bookmark事件，意味着list已完成，否则进行下一次循环重试
		if watchListBookmarkReceived {
			break
		}
    
	}

  // 将store整体替换成接收到的全量数据
	if err := r.store.Replace(temporaryStore.List(), resourceVersion); err != nil {
		return nil, fmt.Errorf("unable to sync watch-list result: %w", err)
	}
  
  // 更新观察到的最大的RV
	r.setLastSyncResourceVersion(resourceVersion)

  // 返回w，后续将使用同一个w进行watch
	return w, nil
}
```

流式list的处理核心点在于对bookmark的识别。

代码省略了一些err处理，当出现err或者本次循环没有收到Bookmark（watchListBookmarkReceived=false），将会在下一次循环进行重试。

### 分块list

不同于watch的持续监听事件流，list的行为是发一次请求，就返回一次数据

```go
func (r *Reflector) list(stopCh <-chan struct{}) error {
  // ...
  
	go func() {
		// 使用pager进行分页读取数据
		pager := pager.New(pager.SimplePageFunc(func(opts metav1.ListOptions) (runtime.Object, error) {
			return r.listerWatcher.List(opts)
		}))
    
    // 决定是否分页、分页的大小
		switch {
		case r.WatchListPageSize != 0:
			pager.PageSize = r.WatchListPageSize
		case r.paginatedResult:
		case options.ResourceVersion != "" && options.ResourceVersion != "0":
			pager.PageSize = 0
		}

    // 开始分块list
		list, paginatedResult, err = pager.ListWithAlloc(context.Background(), options)
    
    // ...
		close(listCh)
	}()
  
	select {
	// ...
	case <-listCh:
	}
  
  // list已结束

  // 此后的list调用一直启用分页（详解见下面第3点）
	if options.ResourceVersion == "0" && paginatedResult {
		r.paginatedResult = true
	}

	//...
  
  // 将store整体替换成接收到的全量数据
	if err := r.syncWith(items, resourceVersion); err != nil {
		return fmt.Errorf("unable to sync list result: %v", err)
	}
	
  // ...
}
```

关于分页这块可以注意一下：

1. 目前没有开放的API让用户设置分页大小，因此分页大小使用默认的大小500（每次最多返回500个对象）
2. 不一定会进行分页，即可能会一次性返回所有对象。原因与[设置的参数/API协议](https://kubernetes.io/zh-cn/docs/reference/using-api/api-concepts/#resource-versions)有关，不过多深究，想了解的话可以看3
3. （TL;DR）首次list时将会使用RV="0"，在数据一致性方面，这意味这返回的数据是非强一致性的，可能是旧一点的数据，但读取效率比较高（CAP权衡中选择了A）；在数据源方面，将会优先从apiserver的watch cache中读取，**watch cache会忽略分页**，一次性返回所有数据，但如果watch cache没有启用，将会从etcd的follower或者leader读取。首次list结束之后，如果发现请求的RV="0"并且数据是以分页的方式返回的，说明watch cache没有启用，并且返回的对象比较多，已经超过了一页，那么将r.paginatedResult设置为true，以后的每次list都会使用分页的方式去拉数据。当RV等于某个精确值时，将始终从watch cache中拉数据，此时禁止分页。当RV=""时，将从etcd拉数据，此时需要使用分页。

分块list的处理核心点在于是否分页以及分页大小

### watch

watchList或list完了之后，就开始持续不断地watch了。在watch的同时，还会周期性地执行resync。resync用于将本地缓存的对象，以Sync事件重新放到DeltaFIFO中，随后会回调OnUpdate通知上层handler进行处理。至于为什么要resync，可以参考[这个问答](https://github.com/cloudnativeto/sig-kubernetes/issues/11)，以及[这篇博客](https://gobomb.github.io/post/whats-resync-in-informer/)。

``` go
// watchWithResync runs watch with startResync in the background.
func (r *Reflector) watchWithResync(w watch.Interface, stopCh <-chan struct{}) error {
	resyncerrc := make(chan error, 1)
	cancelCh := make(chan struct{})
	defer close(cancelCh)
  // 定时resync
	go r.startResync(stopCh, cancelCh, resyncerrc)
  // watch
	return r.watch(w, stopCh, resyncerrc)
}
```

resync不是我们的重点，来看下watch：

``` go
// watch simply starts a watch request with the server.
func (r *Reflector) watch(w watch.Interface, stopCh <-chan struct{}, resyncerrc chan error) error {
	// ...

	for {
		// ...

    // w==nil说明之前可能用的是分块list，主动发起Watch即可
		if w == nil {
			timeoutSeconds := int64(r.minWatchTimeout.Seconds() * (rand.Float64() + 1.0))
			options := metav1.ListOptions{
				ResourceVersion: r.LastSyncResourceVersion(),
				TimeoutSeconds: &timeoutSeconds,
				AllowWatchBookmarks: true,
			}

			w, err = r.listerWatcher.Watch(options)
			
      // ...
		}

    // 持续watch，处理接收到的事件并直接存入store中
		err = handleWatch(start, w, r.store, r.expectedType, r.expectedGVK, r.name, r.typeDescription, r.setLastSyncResourceVersion,
			r.clock, resyncerrc, stopCh)
    
    // ...
    // 错误处理
    // 可能会进行下一轮循环重新建立Watch，或者退出
    
	}
}
```

## controller

这里再放一张架构图来补充之前那张架构图没有体现的 **controller** 的概念。这个controller并不是指用户自定义controller，这里的controller作用是驱动reflector、deltafifo、indexer三者协同运行。对照上方的架构图来说，这里的controller仍然属于上方client-go的那层。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/202510081209373.png)

controller.go文件第一行注释就说明了controller的作用：

``` go
// This file implements a low-level controller that is used in
// sharedIndexInformer, which is an implementation of
// SharedIndexInformer.  Such informers, in turn, are key components
// in the high level controllers that form the backbone of the
// Kubernetes control plane.
```

controller逻辑很简单，就是启动reflector以及运行processLoop方法。之前介绍过，reflector会将监听到的数据送入DeltaFIFO。那么processLoop就是不断从DeltaFIFO取出事件并处理：

``` go
func (c *controller) Run(stopCh <-chan struct{}) {
  // 创建reflector
  r := NewReflectorWithOptions(/*...*/)
	r.WatchListPageSize = c.config.WatchListPageSize

  // ...

  // 启动reflector
	wg.StartWithChannel(stopCh, r.Run)

  // 启动processLoop
	wait.Until(c.processLoop, time.Second, stopCh)
	wg.Wait()
}
```

``` go
func (c *controller) processLoop() {
	for {
    // 不断从DeltaFIFO中pop事件并处理
		obj, err := c.config.Queue.Pop(PopProcessFunc(c.config.Process))
		
    // ...
	}
}
```

处理事件的Process函数是sharedIndexInformer的HandleDeltas方法，后面会说。

## DeltaFIFO

DeltaFIFO顾名思义是一个先进先出队列，队列元素是Deltas，一个Delta是一个对象事件：

``` go
type Deltas []Delta

type Delta struct {
	Type   DeltaType // 在对象上发生的事件，比如新增、删除等
	Object interface{} // 对象
}
```

因此每次从队列pop元素时，是pop对象的在这段时间内发生的所有事件，用张图举个例子：

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20251012234831441.png" alt="image-20251012234831441" style="zoom:50%;" />

按照时间序，在两个对象上发生了四个事件，最终pop的时候是以对象为粒度进行pop，即先将对象1的两个事件作为整体pop，再到对象2。

DeltaFIFO的代码部分就没必要细看了，知道它在Pop元素的时候会处理被pop的元素即可，Pop简化一下大概如下：

``` go
func (f *DeltaFIFO) Pop(process PopProcessFunc) (interface{}, error) {
	f.lock.Lock()
	defer f.lock.Unlock()
	for {
    // 弹出队首元素（Deltas）
		id := f.queue[0]
		f.queue = f.queue[1:]
		item, ok := f.items[id]
		delete(f.items, id)
		
    // 进行处理
		err := process(item, isInInitialList)
		
    // 将元素的所有权移交给外界
		return item, err
	}
}
```

## Indexer

Indexer顾名思义是用来做索引的，索引什么呢？索引对象的key的。比如要获取namespace="default"的所有的pod，用Indexer就能快速得到这些对象的key集合（比如"pod1"、"pod2"这样的字符串集合），然后用这些key访问map获取对象。索引过程有点像倒排索引，即搜索属性为给定值的所有资源，比如namespace就是pod的属性。因此，索引的结构大概是这样的：

```json
{
  // 属性名称
  "namespace": {
    // 属性值 对应的资源
    "default": ["pod1", "pod2"],
    "kube-system": ["pod3"],
  },
  "nodeName": {
    "node-1": ["pod1", "pod3"],
    "node-2": ["pod2"],
  },
}
```

实际上Indexer的本质还是一个存储了所有对象的本地缓存，只不过在这之上提供了索引功能。

（TL;DR: 并且Indexer只是一个接口，它在Store接口基础上提供了索引的相关方法。Indexer的实现是cache，cache又依赖于threadSafeMap提供索引功能。另外，DeltaFIFO所实现的Queue接口其实也是Store...）

## 存储相关接口

说到这里我觉得可以捋一下这些本地存储相关的各个接口的关系，因为看起来还挺乱的。不过具体实现的代码不会去精读，自己扫一眼即可。类图如下：

<img src="https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20251020232406996.png" alt="image-20251020232406996" style="zoom:50%;" />

Store提供了最基础的key-value存储能力，但是要注意这里key是通过value计算出来的，所以可以看到Store的许多方法都只传value不需要传key，比如插入一个对象时，key是通过keyFunc(obj)计算得到的：

``` go
Add(obj interface{}) error
```

另外需要注意的是，Store在实现上并不是一个简单kv存储，key对应的所谓value实际上叫accumulator。accumulator可以被实现为简单的一个obj，即简单kv存储，比如图中的cache；它也可以被实现为对象的集合，比如DeltaFIFO的实现中，accumulator就是一个Deltas。

Indexer和Queue则是在基础存储的基础上，进行了其它能力的扩展。Indexer提供了对key进行索引查找的能力，Queue提供了对象先进先出的能力。

最后再来看看结构体，我们关注的是cache和DeltaFIFO。cache缓存了所有的对象，缓存的是apiserver中的对象，并且cache实现了Indexer提供索引查找的能力。cache的实现很简单，因为具体的存储与索引实现放在了threadSafeMap中。DeltaFIFO虽然实现了Store，但目的不是存下所有对象，只是复用Store提供的方法，比如Add方法实际上类似Push的能力。

## 总结

本文只介绍了informer，利用informer我们可以实时监听资源对象上的增删改事件。而WorkQueue以及后续的处理，就是用户控制器的事情了。

用[operator](https://kubernetes.io/zh-cn/docs/concepts/extend-kubernetes/operator/)那一套来举例：平时基于opeartor那一套去进行开发用户控制器进行集群资源调谐的时候，一般用kubebuilder去生成一些资源对象的深拷贝代码、Reconciler的脚手架代码等，虽然这些代码看起来并没涉及informer、indexer那些东西，但运行起来，当集群资源对象发生变更时，确实会及时回调我们的Reconcile调谐方法，在感知上应该是有informer在运行的。没错，实际上，kubebuilder生成的代码里是用了**controller-runtime**这个库，这个库替开发者封装了informer、workqueue、processitem这些东西，因此开发者只需要定义一个Reconciler，将其注册到controller-runtime这个库中，就OK了（这些kubebuilder生成的代码中也已经做好了，用户只需要编写Reconcile方法，实现自己的业务逻辑）。controller-runtime这个库是k8s的sigs小组开发的，是对client-go进一步封装，让开发者更方便地去开发用户控制器。

## 参考

<https://isekiro.com/categories/client-go/>

<https://github.com/kubernetes/sample-controller/blob/master/docs/controller-client-go.md>

<https://herbguo.gitbook.io/client-go/informer>

<https://github.com/cloudnativeto/sig-kubernetes/issues/11>

<https://gobomb.github.io/post/whats-resync-in-informer/>

<https://kubernetes.io/zh-cn/docs/reference/using-api/api-concepts/#resource-versions>																
