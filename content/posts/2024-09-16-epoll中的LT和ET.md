---
description: 简单描述epoll中LT和ET工作模式的区别和使用场景
title: epoll中的LT和ET
categories: [随笔]
---

本来我在看的是golang的gmp调度器，然后看到注释和代码里面有提到netpoll这个东西，不知不觉又去翻看了下linux网络编程相关的知识，上网找了下博客，找到了ants开源库作者关于go netpoll的博客，而后又因为我不了解其中epoll的LT/ET是什么东西，又赶紧补了一下这些知识，唉，本该早点就了解的知识，一直没能沉下心来看，虽然平时开发业务大概率用不到，但是看稍微底层一点的知识就会涉及到这些东西呢...

## LT和ET的区别

epoll（linux）/kqueue（unix）是现代操作系统用来做I/O多路复用的著名技术，通常有两种不同的工作模式LT（level- triggered,水平触发）和ET（edge-triggered,边缘触发），默认情况下epoll是工作在LT模式下的。

之前摸过一段时间的verilog，很熟悉edge-triggered这个玩意。电平见过吧，1是高0是低，那么一段连续变化的电平，大概可以表示成这个样子：

![image-20240916212607999](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20240916212607999.png)

电平是一段时间内的值，比如横线在上代表这段时间内电平值是1，下面横线代表0，竖线代表由0变1（下降沿，如图中绿色竖线）或者1变0（上升沿，如图中红色竖线）。

这个跟我们的要说的LT和ET有什么关系呢？我们将1代表有fd可读写（对应电平1），0代表没有fd可读写（对应电平0），如果epoll工作在LT模式下，只要fd可读写，那么就`epoll_wait`一定返回有值，否则阻塞，相当于你去询问现在电平是多少，他就返回当前电平的值给你。但如果epoll工作在ET模式下，只有当fd是从不可读写变为可读写的时候（有新事件到来），`epoll_wait`才会告诉你可读写，相当于存储了一个上升沿状态，你`epoll_wait`调用相当于去消费这个状态，消费完后，在下一次边缘到来之前，`epoll_wait`就没有数据返回了，因为这个边缘已经在上一次`epoll_wait`被消费掉了。

举个例子，当前注册fd1和fd2的可读事件到epoll上，初始状态为[0,0]：

1. 工作模式为LT，调用epoll_wait
2. fd1可读事件到达
3. epoll_wait返回fd1可读，状态变成[1,0]
4. 再次调用epoll_wait（注意此时状态依然是[1,0]），立刻返回fd1可读

ET模式：

1. 工作模式为ET，调用epoll_wait
2. fd1可读事件到达，状态变成[1,0]
3. epoll_wait返回fd1可读
4. 再次调用epoll_wait（注意此时状态依然是[1,0]），阻塞

结论就是，在LT工作模式下，可以通过不断询问`epoll_wait`来决定是否要读写fd，因为他总能告诉你最真实的是否可读写的状态。在ET工作模式下，你只会被通知一次fd可以读写，然后你最好就一次性读写完成，相当于将fd的当前状态清除掉，然后才去进行下一次`epoll_wait`，这里说的“一次性读写”并不是说只调用一次`read/write`，而是反复调用直到没有数据可以读出/写入，注意到因为需要反复调用，如果fd是阻塞模型的话，很可能最后一次就阻塞住了，导致你的I/O从多路复用模式直接变成了阻塞模式，甚至这个fd以后可能不再可读写，那你这个程序就再也跑不下去了，所以ET一般和fd设置成非阻塞一起使用。而LT模式下，fd设成阻塞或非阻塞都可以，因为`epoll_wait`会告诉你是否可读写，如果可以读写，`read/write`一定是成功的，因此不会阻塞。

当然，ET还有一些坑需要了解，详情看参考链接

## 参考链接

<https://strikefreedom.top/archives/linux-epoll-with-level-triggering-and-edge-triggering>

<https://strikefreedom.top/archives/go-netpoll-io-multiplexing-reacto>

<https://zhuanlan.zhihu.com/p/40572954>
