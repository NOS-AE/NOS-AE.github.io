---
categories: [源码阅读,go-stdlib]
title: golang Context源码阅读
---



## 前言

- 本文基于`go1.21`，不同版本的`Context`内部实现可能会有细微差别

## 使用场景

为什么需要`Context`，首先思考一个场景：客户端去请求某个服务，这个服务依赖于多个可并行执行的下游服务，为了提高这个服务的响应速度，自然会为每个下游服务开启一个协程去执行。

![image-20240914110641006](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20240914110641006.png)

倘若在执行的过程中，客户端取消了对这个服务的请求，下游服务也应该被停掉。但是go不提供直接关闭协程的方法，我们可以使用`chan`来协作式地关闭这些子协程：在服务代码中创建一个no buffer的`chan`并传递给这些子协程，通过子协程`read chan`+父亲协程`close chan`来达到通知子协程关闭的效果。

我们将这个场景扩展一下：

![image-20240914112147400](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20240914112147400.png)

在这个场景中，child svc1又依赖于两个下游服务，并且某次请求中child svc1因为某些原因会取消对下游服务的请求（不影响其他服务节点的正常运行），老样子，在child svc1中创建一个`chan`并通过这个`chan`去关闭下游协程就好了。child svc1的代码可能会这么写：

```go
func childSvc1(upstream <-chan) {
    downstream := make(chan struct{}, 0)
    defer close(downstream)
    go childSvc1_1(downstream)
    go childSvr1_2(downstream)
  
    for {
        select {
            case <-upstream:
                close(downstream)
                return
            default:
                // 处理业务...

                // 因为某些原因需要取消下游服务
                close(downstream)
        }
    }
}
```

可以看到这段小snippet还能勉强阅读，但是从使用语义上来说，`chan`本身是用于传递数据的，这种`read+close`来关闭协程的方式就是某种hack手段，对于有一定规模的项目来说，这样的使用方式可读性将会非常差并且容易出错。因此我们需要一个带有“取消下游”语义的库来完成这种任务。

`Context`则很好地充当了这样的角色，它不但能控制下游的生命的生命周期，还自带超时控制、取消传递、并发安全、数据传递等功能：

- **取消传递**：父`Context`的取消会自动传递到所有子孙`Context`，使得子孙`Context`也自动取消
- **超时控制**：通过 `context.WithTimeout` 和 `context.WithDeadline`，提供自动超时取消机制
- 数据传递：可以携带请求范围内的数据（比如请求ID、path、begin timestamp等），这个特性在分布式链路追踪框架中被广泛使用

上面第二个场景中的代码换成`Context`来实现：

```go
import "context"

func childSvc1(ctx context.Context) {
    cancel, ctx := context.WithCancel(ctx) // 创建子Context
    defer cancel()
    go childSvc1_1(ctx)
    go childSvr1_2(ctx)

    for {
        select {
            case ctx.Done():
            	return
            default:
                // 处理业务...

                // 因为某些原因需要取消下游服务
                cancel()
        }
    }
}
```

带超时控制的`Context`：

```go
func childSvc1(ctx context.Context) {
  cancel, ctx := context.WithTimeout(ctx, 10 * time.Seconds) // 创建子Context
  defer cancel()
  go childSvc1_1(ctx)
  go childSvr1_2(ctx)
  
  for {
    select {
      case ctx.Done():
      	return
    	default:
      	// 处理业务...

				// 因为某些原因需要取消下游服务
				cancel()
    }
  }
}
```

总的来说`Context`提供了一种统一和标准的方式来处理取消信号和传递请求范围内的数据，使用`Context`可以轻松实现超时和取消功能，并且代码简洁易读。如果使用 channel + close 实现类似功能，代码会更加复杂且容易出错。

这篇文章重点关注 **超时控制和取消传递** 两个功能，至于传递数据功能只需要了解「传递与本次请求相关的一些元数据」即可，一般框架都会给你封装好一些元数据（比如分布式链路追踪框架），这些数据对于业务代码几乎透明，避免滥用。

## 接口定义

`Context`是一个接口，不同的实现对应了不同功能的`Context`，比如超时控制、传递数据等。

```go
type Context interface {
    // 返回超时时间点
    Deadline() (deadline time.Time, ok bool)

    // 返回chan，读这个chan会阻塞，直到Context被取消
    Done() <-chan struct{}

    // 只有两种可能的错误值：
    // Canceled：被取消
    // DeadlineExceeded：超时
    Err() error

    // 取出key对应的value，对应数据传递功能
    Value(key any) any
}
```

四个函数里最神奇的就是这个`Done`方法返回的`chan`，也就是当`Context`被取消之后，无论什么时候读`chan`都不会阻塞，我们可以大胆猜测`Context`也是通过`close chan`来实现的这个效果的。另外`Context`的父亲/祖先`Context`被取消后，这个`Context`也会被取消，下面就看看它是怎么实现的。

## emptyCtx

这个`Context`的实现很有意思，它永远无法取消，也不传递数据，就是纯纯的dummy Context：

```go
type emptyCtx struct{}

func (emptyCtx) Deadline() (deadline time.Time, ok bool) {
	return
}

func (emptyCtx) Done() <-chan struct{} {
	return nil
}

func (emptyCtx) Err() error {
	return nil
}

func (emptyCtx) Value(key any) any {
	return nil
}
```

首先`Context`的所有实现类都是私有的，我们见到的所有`Context`其实都是`context.Background()`，或者是它的子`Context`。而`context.Background()`其实就是`emptyCtx`：

```go
type backgroundCtx struct{ emptyCtx }

func (backgroundCtx) String() string {
	return "context.Background"
}

// Background returns a non-nil, empty [Context]. It is never canceled, has no
// values, and has no deadline. It is typically used by the main function,
// initialization, and tests, and as the top-level Context for incoming
// requests.
func Background() Context {
	return backgroundCtx{}
}
```

另外还有一个`context.TODO()`，使用上效果和`context.Background()`相同，不过语义上有所不同，下面注释也说的很清楚，有时候你要用到`Context`但是上游又没传给你的话，可以先用`context.TODO()`：

```go
type todoCtx struct{ emptyCtx }

func (todoCtx) String() string {
	return "context.TODO"
}

// TODO returns a non-nil, empty [Context]. Code should use context.TODO when
// it's unclear which Context to use or it is not yet available (because the
// surrounding function has not yet been extended to accept a Context
// parameter).
func TODO() Context {
	return todoCtx{}
}
```

## cancelCtx

这个`Context`实现了我们之前提到最核心的功能：**取消传递**

```go
// A cancelCtx can be canceled. When canceled, it also cancels any children
// that implement canceler.
type cancelCtx struct {
	Context

	mu       sync.Mutex            // protects following fields
	done     atomic.Value          // of chan struct{}, created lazily, closed by first cancel call
	children map[canceler]struct{} // set to nil by the first cancel call
	err      error                 // set to non-nil by the first cancel call
	cause    error                 // set to non-nil by the first cancel call
}
```

`cancelCtx`继承了父`Context`的`Deadline`方法，并重新实现了`Context`接口的其他三个方法（这三个方法的实现都很普通，该返回什么就返回什么）。

用`context.WithCancel`创建子`Context`时，最终会调用`withCancel`来创建`cancelCtx`：

```go
func withCancel(parent Context) *cancelCtx {
	if parent == nil {
		panic("cannot create context from nil parent")
	}
	c := &cancelCtx{}
	c.propagateCancel(parent, c)
	return c
}
```

`cancelCtx.propagateCancel`将节点与父节点绑定，当父节点取消时当前节点也一并取消，实现取消传递机制：

```go
// propagateCancel arranges for child to be canceled when parent is.
// It sets the parent context of cancelCtx.
func (c *cancelCtx) propagateCancel(parent Context, child canceler) {
	c.Context = parent
  
	...
  
	if p, ok := parentCancelCtx(parent); ok {
		// parent is a *cancelCtx, or derives from one.
    p.mu.Lock()
    
    ...

		p.children[child] = struct{}{}
    p.mu.Unlock()
		return
	}

	...

	goroutines.Add(1)
	go func() {
		select {
		case <-parent.Done():
			child.cancel(false, parent.Err(), Cause(parent))
		case <-child.Done():
		}
	}()
}

```

1. 如果某个最近祖先节点是`cancelCtx`，加入这个节点`children`中，当祖先节点取消的时候将其`children`也取消
2. 开启一个协程，监听父亲节点取消和子节点取消，当父亲节点取消的时候将该子节点也取消

回到`context.WithCancel`函数：

```go
func WithCancel(parent Context) (ctx Context, cancel CancelFunc) {
	c := withCancel(parent)
	return c, func() { c.cancel(true, Canceled, nil) }
}
```

返回的`CancelFunc`函数里调用的是`cancelCtx.cancel`，这个方法取消`cancelCtx`并取消所有的`children`：

```go
// cancel closes c.done, cancels each of c's children, and, if
// removeFromParent is true, removes c from its parent's children.
// cancel sets c.cause to cause if this is the first time c is canceled.
func (c *cancelCtx) cancel(removeFromParent bool, err, cause error) {
    ...

    c.mu.Lock()
    ...
  
	d, _ := c.done.Load().(chan struct{})
	if d == nil {
		c.done.Store(closedchan)
	} else {
		close(d)
	}
	for child := range c.children {
		child.cancel(false, err, cause)
	}
	c.children = nil
	c.mu.Unlock()

	if removeFromParent {
		removeChild(c.Context, c)
	}
}
```

1. 取消当前节点，即关闭`c.done`
2. 调用`cancel`级连取消每个子节点
3. 将节点从父节点的`children`移出

这里的`removeFromParent`是个优化操作：

- 当父节点被取消，此时肯定会取消所有子节点并一次性丢弃整个`children`，不用再一个个地从`children`移出，`removeFromParent`设为`false`。

  ![image-20240914173448893](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20240914173448893.png)

- 当子节点本身主动取消时才传入`true`，从父节点的`children`移除。

![image-20240914173638589](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20240914173638589.png)

## timerCtx

`timerCtx`在`cancelCtx`的基础上，增加超时自动取消的功能。

```go
type timerCtx struct {
	cancelCtx
	timer *time.Timer // Under cancelCtx.mu.

	deadline time.Time
}
```

注意这里`cancelCtx`成员不是父节点，这是一个内嵌结构体而不是接口/指针，表示`timerCtx`也是一个`cancelCtx`，类似于继承关系

`context.WithDeadlineCause`是构造`timerCtx`的入口函数：

```go
func WithDeadlineCause(parent Context, d time.Time, cause error) (Context, CancelFunc) {
    ...
  
	c := &timerCtx{
		deadline: d,
	}
	c.cancelCtx.propagateCancel(parent, c)
	
    ...
  
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err == nil {
		c.timer = time.AfterFunc(dur, func() {
			c.cancel(true, DeadlineExceeded, cause)
		})
	}
	return c, func() { c.cancel(true, Canceled, nil) }
}
```

1. 调用`cancelCtx.propagateCancel`实现取消传递机制
2. 调用`time.AfterFunc`实现超时取消机制

`timerCtx`还需要在`cancel`函数中将`timer`停掉，避免资源泄漏

```go
func (c *timerCtx) cancel(removeFromParent bool, err, cause error) {
	c.cancelCtx.cancel(false, err, cause)
    ...
  
	c.timer.Stop()
    ...
}
```

## valueCtx

`valueCtx`用于实现在`Context`中携带数据，以在各个服务之间通过`Context`传递数据。

```go
type valueCtx struct {
	Context
	key, val any
}
```

一个`valueCtx`只携带一对key-value，对应入口函数`context.WithValue`传入的key, value。

调用`Context.Value`获取value时，其实是从本节点开始往根节点的方向，找到第一个匹配key的`valueCtx`，并将其value取出。

![image-20240914172138150](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/image-20240914172138150.png)

比如我们在`valueCtx(key=1)`上调用`Value(2)`，会向上找到`valueCtx(key=2)`，取出它的value并返回。

## withoutCancelCtx

顾名思义，`withoutCancelCtx`断开了父节点的取消传递，当父节点取消时不会取消这个节点及其子节点，相当于想要父节点携带的数据，但不想要父节点的取消传递，实用且简单。

## afterFuncCtx

`afterFuncCtx`用于当父节点取消的那一刻触发一次函数的调用

```go
type afterFuncCtx struct {
	cancelCtx
	once sync.Once // either starts running f or stops f from running
	f    func()
}
```

与一般的`context.With...`不同，`afterFuncCtx`是用`context.AfterFunc`构造的：

```go
func AfterFunc(ctx Context, f func()) (stop func() bool) {
	a := &afterFuncCtx{
		f: f,
	}
	a.cancelCtx.propagateCancel(ctx, a)
	return func() bool {
		stopped := false
		a.once.Do(func() {
			stopped = true
		})
		if stopped {
			a.cancel(true, Canceled, nil)
		}
		return stopped
	}
}
```

这里巧妙地用到`sync.Once`最多执行一次的特性，如果外界在父节点取消前手动调用`stop`，那么传入的`f`就不会在父节点取消后被调用，并且该`afterFuncCtx`被取消。

否则，如果是父节点先被取消，进而调用`afterFuncCtx.cancel`，那么`f`会被调用：

```go
func (a *afterFuncCtx) cancel(removeFromParent bool, err, cause error) {
	a.cancelCtx.cancel(false, err, cause)
	if removeFromParent {
		removeChild(a.Context, a)
	}
	a.once.Do(func() {
		go a.f()
	})
}
```

## stopCancel

没理解用来干啥的
