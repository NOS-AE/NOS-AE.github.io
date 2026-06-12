---
date: 2026-04-12T23:51:04+08:00
title: Golang网络标准库阅读
tags: [源码阅读,go标准库,网络,net]
categories: [源码阅读]
draft: true
---

> [!note]
>
> 基于 go 1.25, GOOS = linux, GOARCH = arm64
>
> 忽略不关键的 err 处理和 if/case path

## 前言

go 基于 I/O 多路复用、non-blokcing I/O 以及 GMP 构建了一个简洁而高性能的网络模型 netpoller，netpoller 在不同操作系统上的实现有所不同，但其核心思想以及最终暴露给上层开发者的接口都是相同的，极大地降低了开发者编写网络应用时的心智负担。

本篇就基于 linux 操作系统，深入源码来看看 go 如何实现 netpoller。

首先来了解一些背景知识：

### 用户/内核态、用户/内核空间

现代操作系统都会把虚拟地址空间分成两部分：内核空间与用户空间，内核空间在低地址，剩下的是用户空间。不同进程的用户空间虽然一样，但在物理内存上彼此隔离。而内核空间都映射到同一块物理内存上，被各个进程共享。

再来说说用户态和内核态，其实这里说的是权限。我们运行的程序一般处于用户态，此时不能执行特权指令、访问硬件、关中断等操作，不然很容易把操作系统搞崩溃。但往往一个程序的执行很可能需要访问硬件或者执行其他危险操作，所以需要切换到内核态中去执行（所谓 "切换" 指的不是调一个函数进入内核态，然后就能随便执行特权指令了。简单来说，是往某个寄存器写某个值，随后硬件才真正切换到内核态，随后程序计数器下一步陷入/跳转到已经写死的一段特权代码中，执行那些硬件读写操作，这一个过程在用户态看来是原子操作）。

而且，用户态内核态的切换不仅仅是写一个寄存器那么简单，出于操作系统对用户态的不信任，往往还会执行一些安全检查。然后，还设计保存完整上下文、甚至切换页表等开销。最常见的，I/O 系统调用，刚刚我们提到，整个系统调用的过程在用户态看来是一个原子操作，这意味着在完成之前 syscall 不会返回，所以操作系统往往还给文件提供了 non-blocking I/O 模式，这个点很重要，我们后面会说到。

### I/O 模型

操作系统的 I/O 操作，以网络 I/O 为例，通常包含两个步骤：

1. 等待网络数据到达网卡(读就绪)/等待网卡可写(写就绪) –> 读取/写入到内核缓冲区
2. 从内核缓冲区复制数据 –> 用户空间(读)/从用户空间复制数据 -> 内核缓冲区(写)  

从用户态的视角看来，整个 I/O 可以归纳为 5 种类别：

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/5-io-model.jpg)

上述 5 种又可以根据「用户空间与内核空间之间的数据拷贝是否阻塞当前进程」分为同步和异步 I/O，可以看到除了第五种是异步的，其它全都是同步 I/O。

本篇更关注非阻塞 I/O。非阻塞指的是所有 I/O 操作都是立刻返回不会阻塞用户进程，然后返回 EGAIN 错误，让你再次进行 I/O 操作，直到内核数据准备好。但是我们程序不可能专门开一个线程来一直轮询这个 I/O 操作，这样还不如直接进行阻塞 I/O。所以，非阻塞 I/O 一般是配合 I/O 多路复用才能发挥出最大的效果。

### I/O 多路复用

所谓 I/O 多路复用指的就是 select/poll/epoll 这一系列的多路选择器：支持单一线程同时监听多个文件描述符（I/O 事件），阻塞等待，并在其中某个文件描述符可读写时收到通知。**因此 I/O 复用其实是复用线程，让单线程也能高效处理多个连接（I/O 事件）**

select 是 epoll 出现之前的多路复用技术。理解 select 的关键在于理解 fd_set，为说明方便，取 fd_set 长度为 1 字节，fd_set 中的每一 bit 可以对应一个文件描述符 fd，则 1 字节长的 fd_set 最大可以对应 8 个 fd。select 的调用过程如下：

1. 执行 FD_ZERO(&set), 则 set 用位表示是 0000,0000
2. 若 fd＝5, 执行 FD_SET(fd, &set) 后 set 变为 0001,0000(第 5 位置为 1)
3. 再加入 fd＝2, fd = 1，则 set 变为 0001,0011
4. 执行 select(6, &set, 0, 0, 0) 阻塞等待
5. 若 fd = 1, fd = 2 上都发生可读事件，则 select 返回，此时 set 变为 0000,0011 (注意：没有事件发生的 fd = 5 被清空)  

注意到 select 有以下缺点：

1. 最大并发限制，受限于 set 的最大长度
2. 每次调用 select 都将整个 set 在内核态和用户态之间拷贝，开销较大
3. 每次 I/O 事件发生，都要线性扫描 set。当监听的 fd 比较多时 CPU 开销也大

后面还有 poll 系统调用，它相比 select 只是解决了最大并发限制的问题，但 2、3 点并没解决

epoll 是 linux2.6 之后引入的新 I/O 事件驱动技术。先看一下 epoll 的 API 设计：

``` c
#include <sys/epoll.h>
// 创建一个epoll fd
int epoll_create(int size); // int epoll_create1(int flags);
// 注册fd所等待的事件到epoll fd上
int epoll_ctl(int epfd, int op, int fd, struct epoll_event *event);
// 等待事件发生，将发生的事件拷贝到用户空间的内存地址events上并返回
int epoll_wait(int epfd, struct epoll_event *events, int maxevents, int timeout);
```

epoll 工作原理如图：

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/epoll-principle.png)

`epoll_ctl` 注册 fd 的事件到内核时，只需要一次用户空间到内核空间的拷贝，将注册的 fd 及其事件挂到到内核空间中的属于该 epoll fd 的一颗 **红黑树** 上，解决了 select 的问题 2。注意，fd 虽然叫做文件描述符，但其背后代表的是一个设备（比如网卡）。当我们注册 fd 的事件时，会在相应的设备驱动程序那里将当前进程注册到等待队列中，后续当这个设备的 I/O 事件到来的时候，就会唤醒进程并将就绪设备的信息以及 fd 拷贝到 **就绪设备链表** 中，`epoll_wait` 读取绪设备链表，用 fd 查询红黑树，看看用户期待的 I/O 是不是就是这个设备事件，如果是的话就拷贝到入参 events 中并返回，用户程序就可以读取 I/O 事件了，解决了 select 的问题 3。

## 创建一个 HTTP 服务器

学发动机原理前先学开车。在进入 golang net 源码之前，看看我们平时是怎么启动一个服务器的。然后慢慢地自上而下地稍微看一点源码，从而引出我们重点要讲的 golang netpoller。

在 go 语言中，创建一个最小服务器仅需几行：

``` go
http.HandleFunc("/", func(writer http.ResponseWriter, request *http.Request) {
  writer.Write([]byte("hi"))
})
http.ListenAndServe(":8080", nil)
```

其实这只不过是快速起服务器的语法糖。实际上我们往往还要传入一些 tcp 配置、http 配置等，完整代码如下：

``` go
// 配置HTTP路由
http.HandleFunc("/", func(writer http.ResponseWriter, request *http.Request) {
    fmt.Println("hi")
})
// 配置tls、tcp、http2、日志等
server := http.Server{
    DisableGeneralOptionsHandler: false,
    TLSConfig:                    nil,
    ReadTimeout:                  0,
    ReadHeaderTimeout:            0,
    WriteTimeout:                 0,
    IdleTimeout:                  0,
    MaxHeaderBytes:               0,
    TLSNextProto:                 nil,
    ConnState:                    nil,
    ErrorLog:                     nil,
    BaseContext:                  nil,
    ConnContext:                  nil,
    HTTP2:                        nil,
    Protocols:                    nil,
}
// 配置listener
lc := net.ListenConfig{
    Control:         nil,
    KeepAlive:       0,
    KeepAliveConfig: net.KeepAliveConfig{},
}
// 开始listen
listener, err := lc.Listen(context.Background(), "tcp", ":8080")
if err != nil {
    panic(err)
}
// 开始serve
panic(server.Serve(listener))
```

## Listen

`Listen` 在 `dial.go` 文件中

``` go
// 监听address地址
// network必须是"tcp", "tcp4", "tcp6", "unix" or "unixpacket"。注意到这里是不支持ip和udp的，因为Listen是为了建立面向连接的服务
func (lc *ListenConfig) Listen(ctx context.Context, network, address string) (Listener, error) {
  // 解析host为addr，比如localhost:http解析为[::1]:80和127.0.0.1:80
	addrs, err := DefaultResolver.resolveAddrList(ctx, "listen", network, address, nil)
	sl := &sysListener{
		ListenConfig: *lc,
		network:      network,
		address:      address,
	}
	var l Listener
  // 从addrs中优先选ipv4，不然就返回addrs中的第一个
	la := addrs.first(isIPv4)
	switch la := la.(type) {
	case *TCPAddr:
		if sl.MultipathTCP() {
			l, err = sl.listenMPTCP(ctx, la)
		} else {
      // listenTCP开始监听TCP（我们不关注MPTCP）
			l, err = sl.listenTCP(ctx, la)
		}
	}
	return l, nil
}

func (sl *sysListener) listenTCP(ctx context.Context, laddr *TCPAddr) (*TCPListener, error) {
	return sl.listenTCPProto(ctx, laddr, 0)
}
```

listenTCP 中会调用 `listenTCPProto(ctx, laddr, 0)`，在 `tcpsock_posix.go` 中：

``` go
func (sl *sysListener) listenTCPProto(ctx context.Context, laddr *TCPAddr, proto int) (*TCPListener, error) {
	...
  // 创建netFD
	fd, err := internetSocket(ctx, sl.network, laddr, nil, syscall.SOCK_STREAM, proto, "listen", ctrlCtxFn)
  // 将netFD包装在TCPListener中
	return &TCPListener{fd: fd, lc: sl.ListenConfig}, nil
}
```

`internetSocket` 在 `ipsock_posix.go` 中：

``` go
func internetSocket(ctx context.Context, net string, laddr, raddr sockaddr, sotype, proto int, mode string, ctrlCtxFn func(context.Context, string, string, syscall.RawConn) error) (fd *netFD, err error) {
  // 决定协议族（AF_INET或AF_INET6）以及是否ipv6only
	family, ipv6only := favoriteAddrFamily(net, laddr, raddr, mode)
  // 创建netFD
	return socket(ctx, net, family, sotype, proto, ipv6only, laddr, raddr, ctrlCtxFn)
}
```

`socket` 在 `sock_posix.go` 中：

``` go
// 创建socket，初始化socket，将其包装在netFD并返回
// 还负责初始化netpoller
func socket(ctx context.Context, net string, family, sotype, proto int, ipv6only bool, laddr, raddr sockaddr, ctrlCtxFn func(context.Context, string, string, syscall.RawConn) error) (fd *netFD, err error) {
  // socket syscall创建socket
	s, err := sysSocket(family, sotype, proto)
  // setsockopts syscall设置socket支持双栈（ipv4+ipv6）
	if err = setDefaultSockopts(s, family, sotype, ipv6only); err != nil {
		poll.CloseFunc(s)
		return nil, err
	}
  // 将socket fd包装在netFD中
	if fd, err = newFD(s, family, sotype, net); err != nil {
		poll.CloseFunc(s)
		return nil, err
	}

  // 作为服务端，调用netFD.listenStream进一步设置
	fd.listenStream(ctx, laddr, listenerBacklog(), ctrlCtxFn)
}
  
func (fd *netFD) listenStream(ctx context.Context, laddr sockaddr, backlog int, ctrlCtxFn func(context.Context, string, string, syscall.RawConn) error) error {
	var err error
  // setsockopts syscall设置SO_REUSEADDR=1，以便重启时快速恢复监听
	if err = setDefaultListenerSockopts(fd.pfd.Sysfd); err != nil {
		return err
	}
	var lsa syscall.Sockaddr
	if lsa, err = laddr.sockaddr(fd.family); err != nil {
		return err
	}

  // bind syscall将声明ip:port的占用，此时socket进入CLOSED状态
	syscall.Bind(fd.pfd.Sysfd, lsa)

  // listen syscall将分配资源（半连接队列/全连接队列等）以及开始监听SYN，此时socket进入LISTEN状态
	listenFunc(fd.pfd.Sysfd, backlog)

  // netFD.init主要是用来初始化netpoller
	if err = fd.init(); err != nil {
		return err
	}
	lsa, _ = syscall.Getsockname(fd.pfd.Sysfd)
  // 将laddr和raddr设置到fd上，并设置finalizer，保证底层连接不再被使用时能正确地被关闭
	fd.setAddr(fd.addrFunc()(lsa), nil)
	return nil
}
```

`sock_cloexec.go`

``` go
func sysSocket(family, sotype, proto int) (int, error) {
  // 设置socket属性：
  // SOCK_NONBLOCK（非阻塞I/O）以及SOCK_CLOEXEC（切换到子进程时关闭fd）
	s, err := socketFunc(family, sotype|syscall.SOCK_NONBLOCK|syscall.SOCK_CLOEXEC, proto)
	return s, nil
}
```

值得注意的是 socket 具有了 non-blocking 属性，避免网络 I/O 操作陷入到内核态，然后借助 GMP 模型，将网络 I/O 的相关调度也牢牢地掌握在用户态中。最后，再搭配多路复用机制，打造 golang 的 netpoll 网络模型，使得我们可以轻松开发出高性能网络应用。

关于 `Listen` 我们不用 DFS 太深入。它大概干了这几件事：

1. 解析要监听的协议、地址、端口等信息
2. 创建与初始化 socket，将其包装在 netFD 中
3. 初始化 netFD 与 netpoller

## netpoller 核心数据结构

go netpoller 模型其实是对底层 epoll/kqueue/iocp 的封装。我们以 linux 平台的 epoll 为例进行源码分析。如果你是 mac 或者 Windows 的机器你还可以利用 debugger 分析一下 kqueue 或 iocp，其实是大同小异的，在这里不多讨论。

总的来说，golang 所有的网络操作都以封装的的一个结构体 netFD 为中心进行实现。netFD 封装了 pollDesc 结构体，当 netFD 上读写遇到 EAGAIN 错误的时候就将当前 goroutine 存储到这个 netFD 对应的 pollDesc 中，同时调用 gopark 将当前 goroutine 给阻塞住（用户态的阻塞）。之后当这个 netFD 上通过 epoll 得知这个 netFD 有新来的读写事件发生时，就会将这个 goroutine 重新 "放出来" 运行。

netFD 是一个网络描述符，类似于 linux 的文件描述符，netFD 是为了实现 netpoller 而做的一个更高级的封装：

``` go
// Network file descriptor.
type netFD struct {
	pfd poll.FD

	// immutable until Close
	family      int
	sotype      int
	isConnected bool // handshake completed or use of association with peer
	net         string
	laddr       Addr
	raddr       Addr
}
```

netFD 中包含了一个 poll.FD 结构体，其中包含了 Sysfd 和 pollDesc，前者是真正的系统文件描述符，而后者封装了底层事件驱动，所有的读写超时操作都是通过调用后者的方法实现：

``` go
// FD is a file descriptor. The net and os packages use this type as a
// field of a larger type representing a network connection or OS file.
type FD struct {
	// Lock sysfd and serialize access to Read and Write methods.
	fdmu fdMutex

	// System file descriptor. Immutable until Close.
	Sysfd int

	// Platform dependent state of the file descriptor.
	SysFile

	// I/O poller.
	pd pollDesc

	// Semaphore signaled when file is closed.
	csema uint32

	// Non-zero if this file has been set to blocking mode.
	isBlocking uint32

	// Whether this is a streaming descriptor, as opposed to a
	// packet-based descriptor like a UDP socket. Immutable.
	IsStream bool

	// Whether a zero byte read indicates EOF. This is false for a
	// message based socket connection.
	ZeroReadIsEOF bool

	// Whether this is a file rather than a network socket.
	isFile bool
}
```

pollDesc：

```go
type pollDesc struct {
    runtimeCtx uintptr
}
```

pollDesc 里只有一个指针 `runtimeCtx`，它实际指向的是 src/runtime 包中的 pollDesc 结构体，这个结构体非常重要，是结合 GMP 调度器实现高性能 netpoller 的关键：

```go
// Network poller descriptor.
//
// No heap pointers.
type pollDesc struct {
	_     sys.NotInHeap
	link  *pollDesc      // in pollcache, protected by pollcache.lock
	fd    uintptr        // constant for pollDesc usage lifetime
	fdseq atomic.Uintptr // protects against stale pollDesc

	// atomicInfo holds bits from closing, rd, and wd,
	// which are only ever written while holding the lock,
	// summarized for use by netpollcheckerr,
	// which cannot acquire the lock.
	// After writing these fields under lock in a way that
	// might change the summary, code must call publishInfo
	// before releasing the lock.
	// Code that changes fields and then calls netpollunblock
	// (while still holding the lock) must call publishInfo
	// before calling netpollunblock, because publishInfo is what
	// stops netpollblock from blocking anew
	// (by changing the result of netpollcheckerr).
	// atomicInfo also holds the eventErr bit,
	// recording whether a poll event on the fd got an error;
	// atomicInfo is the only source of truth for that bit.
	atomicInfo atomic.Uint32 // atomic pollInfo

	// rg, wg are accessed atomically and hold g pointers.
	// (Using atomic.Uintptr here is similar to using guintptr elsewhere.)
	rg atomic.Uintptr // pdReady, pdWait, G waiting for read or pdNil
	wg atomic.Uintptr // pdReady, pdWait, G waiting for write or pdNil

	lock    mutex // protects the following fields
	closing bool
	rrun    bool      // whether rt is running
	wrun    bool      // whether wt is running
	user    uint32    // user settable cookie
	rseq    uintptr   // protects from stale read timers
	rt      timer     // read deadline timer
	rd      int64     // read deadline (a nanotime in the future, -1 when expired)
	wseq    uintptr   // protects from stale write timers
	wt      timer     // write deadline timer
	wd      int64     // write deadline (a nanotime in the future, -1 when expired)
	self    *pollDesc // storage for indirect interface. See (*pollDesc).makeArg.
}
```

注意到，poll.FD 与 pollDesc 都位于 src/internal/poll 包中，后者的方法实现基本上都是 runtime_xxx 方法的封装，而 runtime_xxx 只有函数签名却没有函数体（internal 包中的 pollDesc 与其方法在 fd_poll_runtime.go 中）：

``` go
func runtime_pollServerInit()
func runtime_pollOpen(fd uintptr) (uintptr, int)
func runtime_pollClose(ctx uintptr)
func runtime_pollWait(ctx uintptr, mode int) int
func runtime_pollWaitCanceled(ctx uintptr, mode int)
func runtime_pollReset(ctx uintptr, mode int) int
func runtime_pollSetDeadline(ctx uintptr, d int64, mode int)
func runtime_pollUnblock(ctx uintptr)
func runtime_isPollServerDescriptor(fd uintptr) bool
```

他们的具体实现位于 src/runtime/netpoll.go 中，通过 go: linkname 将其链接到这些方法签名中。我第一次看到这样的代码时很疑惑，为什么不直接将 runtime 中的 pollDesc 以及这些函数实现都放到 internal 中，而是通过指针、go: linkname 去引用。原因大致如下：

netpoller 是与 GMP 调度器高度相关的，并且 GMP 是在 runtime 中实现的。而 internal 包作为内部工具包，是被其他包单向调用的，不会出现 internal 又引用 runtime 的情况，否则就循环依赖了。internal 包屏蔽了许多 fd 相关的平台差异（看 src/internal/poll 中的文件命名就知道）。为了将 internal 包的 netpoller 相关代码编织到调度器中，go 通过使用 go: linkname 将 runtime 包中的函数绑定到 internal 包的这些函数声明，最终做到了 internal 包调用这些函数时，在汇编层面其实是直接跳转到了 runtime 中的函数的地址，这样既能无需注册回调函数保持运行时高效率，又能避免循环引用问题。

从整个调用链来看：

```
net/os
  -> internal/poll.FD
      -> runtime_pollOpen / runtime_pollWait / runtime_pollSetDeadline
          -> runtime netpoll backend: epoll/kqueue/IOCP/etc.
              -> scheduler parks/wakes goroutines
```

net 和 os 包共享 internal 包对文件描述符 fd 的封装，包括引用计数、读写锁、Accept, ReadFrom, WriteMsg, sendfile, splice, socket options 之类的处理。而调度的部分只有 runtime 包能处理，包括 gopark、goready、让空闲调度器阻塞在 netpoll 等操作。所以，internal 和 runtime 其实都涉及了一些平台相关的代码，但他们的目的不一样：

- runtime/netpoll_*.go: 平台相关的 netpoll 机制，比如 epoll/kqueue/IOCP，被 GMP 用到
- internal/poll/*.go: 平台相关的文件描述符/句柄处理，常常被 net/os 包用到

## netpoller 核心方法

### netFD.init

之前在 net.Listen 方法中，我们看到一系列系统调用（socket, listen, bind）完成后，有一行 fd.init 方法，这个方法就是将 socket fd 与 netpoller 关联起来的核心方法：

``` go
func (fd *netFD) init() error {
	return fd.pfd.Init(fd.net, true)
}
```

实际调用的是 poll.FD.Init（平台相关）：

``` go
// 初始化 poll.FD
// net参数是网络名称比如"tcp"，或者"file"
// 如果pollable=true，那么这个fd由netpoller管理（一般只要fd是非阻塞的，pollable都是true）
func (fd *FD) Init(net string, pollable bool) error {
	fd.SysFile.init()

  // "file"类型比较特殊，标记一下，后面异常的时候方便打日志。但实际上是不是"file"并不影响netpoller的执行逻辑
	if net == "file" {
		fd.isFile = true
	}
	if !pollable {
		fd.isBlocking = 1
		return nil
	}
  // 初始化pollDesc
	err := fd.pd.init(fd)
	if err != nil {
		// If we could not initialize the runtime poller,
		// assume we are using blocking mode.
		fd.isBlocking = 1
	}
	return err
}
```

pollDesc.init（平台无关）调用了 runtime 包中的 runtime_pollServerInit，并且注意到用了 sync.Once 来调用这个函数，说明整个运行时只会运行一遍 runtime_pollServerInit。然后调用 runtime_pollOpen 用来，返回一个 runtime.pollDesc 指针，在 internal 中保存起来，以后作为其它 runtime netpoll 相关函数的入参：

``` go
func (pd *pollDesc) init(fd *FD) error {
  // 初始化netpoll backend（指的是epoll/kqueue/iocp）
	serverInit.Do(runtime_pollServerInit)
  // 
	ctx, errno := runtime_pollOpen(uintptr(fd.Sysfd))
	if errno != 0 {
		return errnoErr(syscall.Errno(errno))
	}
	pd.runtimeCtx = ctx
	return nil
}
```



## 参考

https://strikefreedom.top/archives/go-netpoll-io-multiplexing-reactor
