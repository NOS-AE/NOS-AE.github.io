## 前言

sync包提供了常见的并发编程工具，比如最常见的Mutex、WaitGroup等。这些工具都非常简洁，几乎0学习成本。本篇将从源码角度简单看看这些工具的实现原理，以在未来有需求的时候，理解甚至是手动实现功能更强大的，更复杂的并发编程工具。

## sync.Mutex

`sync.Mutex`是golang中的互斥锁，但是注意它仅仅具有互斥访问的功能，没有其他功能，比如不支持可重入、不可自定义公平/非公平。

### 公平性

对于公平性，Mutex采取了综合两者的做法：

- normal mode（非公平模式，利于高效率运行）：锁释放时，优先让同时新来尝试获取锁的线程获取到锁，而不是等待队列中的线程，运行成本低，只需数次CAS就能获取到锁。这是默认的模式
- starvation mode（公平模式，避免高并发下线程饿死）：锁释放时，优先让等待队列的线程获取到锁，而不是新来的线程。当等待队列队头线程等待超过1ms进入公平模式

如果当前为公平模式，那么当等待队列唯一的队头线程获取到锁，或者队头线程等待时间不足1ms，又会自动回到非公平模式。

### 可重入性

在开始源码之前，关于为什么golang的官方互斥锁不考虑支持可重入性我想简单讨论下。Russ Cox在[讨论](https://groups.google.com/g/golang-nuts/c/XqW1qcuZgKg/m/Ui3nQkeLV80J)里核心观点在于：互斥锁的目的是保护程序的不变性（即invariant，关于什么是程序的不变性可以参考[这篇](https://stackoverflow.com/questions/112064/what-is-an-invariant)）。因此当线程获取到互斥锁以及释放锁的那一刻，程序都应该是invariant的，在持有锁的期间，程序可以随便破坏invariant，只要保证释放锁的那一刻恢复了invariant即可。从这个观点来说，如果锁是可重入的，就会有这样的情况发生：

```go
func G() {
    mu.Lock()
    // 破坏invariant
    ...
    F()
    
    // 恢复invariant
    ...
    mu.Unlock()
}

func F() {
    mu.Lock()
    // 此时持有锁，程序应该是invariant的
    // 继续执行下去可能会导致bug，因为F认为持有锁的那一刻程序是invariant的
    // 但F不知道invariant已经被G破坏
    ...
    mu.Unlock()
}
```

也就是说，Russ Cox给互斥锁功能上的定义是保持程序的invariant，因此可重入锁的想法就是错的。但也有别的观点认为他对互斥锁的定义是错的，互斥锁本身就是为了避免多线程访问修改变量，invariant是开发者的责任，与你用不用互斥锁无关，互斥锁只是帮助你实现invariant的，并且出于编程上的方便，可重入锁可以make your life easier！况且很多语言其实都支持可重入锁。

另外关于invariant，本人也不认为是互斥锁的责任，比如在单线程的程序中，你需要维护`(a==b)==true`这个invariant，而且由于单线程你根本不需要锁，那么只要你会改变a或者改变b，就肯定会有某些时刻会出现invariant被破坏的情况，但这些情况一般是函数内部的瞬时发生的，而函数执行前后都是保持invariant的就没问题，可以看到与锁无关。因此锁只是个实现invariant的工具之一，它只需要关注底层并发的事情，不需要给他下“保持程序的invariant”这样的高层抽象定义。

后面我们会利用sync包现有的工具，尝试实现一个可重入锁。

### 源码

Mutex结构体：

```go
const (
	mutexLocked = 1 << iota // 锁是否被线程持有
	mutexWoken // 是否有被唤醒的线程正在等待获取锁
	mutexStarving // 是否处于饥饿模式
)

type Mutex struct {
	state int32 // 低三位分别对应上述三个状态，高位记录等待队列的线程数
	sema  uint32 // 给底层同步原语使用的信号量
}
```



Lock方法：

```go
func (m *Mutex) Lock() {
	// Fast path: 使用CAS快速加锁
	if atomic.CompareAndSwapInt32(&m.state, 0, mutexLocked) {
		if race.Enabled {
			race.Acquire(unsafe.Pointer(m))
		}
		return
	}
    // Slow path: CAS加锁失败，说明锁被其他线程占有，当前应该被阻塞
	m.lockSlow()
}
```

lockSlow方法：

```go
func (m *Mutex) lockSlow() {
	var waitStartTime int64
	starving := false // 线程饥不饥饿
	awoke := false // 线程是不是唤醒状态
	iter := 0 // 自旋次数
	old := m.state
	for {
        // 如果不饥饿，表明是非公平模式，尝试自旋等待锁释放
		// 自选若干次，等到达到最大自旋次数或者锁释放就停止自旋
		if old&(mutexLocked|mutexStarving) == mutexLocked && runtime_canSpin(iter) {
            // 如果当前没有其它醒着的线程，当前线程视为醒着，让Unlock的线程不要唤醒阻塞的线程
			if !awoke && old&mutexWoken == 0 && old>>mutexWaiterShift != 0 &&
				atomic.CompareAndSwapInt32(&m.state, old, old|mutexWoken) {
				awoke = true
			}
			runtime_doSpin()
			iter++
			old = m.state
			continue
		}
		new := old
        // 如果不饥饿，表明是非公平模式，当前线程可以去竞争锁
		if old&mutexStarving == 0 {
			new |= mutexLocked
		}
        // 如果有锁或者饥饿，表明当前线程将会阻塞等待锁释放，waiter+1
		if old&(mutexLocked|mutexStarving) != 0 {
			new += 1 << mutexWaiterShift
		}
		// 如果需要饥饿，并且有锁，那么设为饥饿
        // 为什么锁已经释放的情况下不能设为饥饿，首先waiter只会由Unlock来唤醒，如果锁已经被释放了，而这里又设成饥饿变成公平锁，新来的线程直接进入等待队列被阻塞，并且原来的waiter因为阻塞也不可能获取到锁，最终导致没有线程来Unlock
		if starving && old&mutexLocked != 0 {
			new |= mutexStarving
		}
        // 这个线程可能即将会通过CAS直接获取到锁或者进入阻塞，将唤醒状态设回0
		if awoke {
			if new&mutexWoken == 0 {
				throw("sync: inconsistent mutex state")
			}
			new &^= mutexWoken
		}
        // 尝试CAS将state设成new
		if atomic.CompareAndSwapInt32(&m.state, old, new) {
            // 通过CAS成功获取到锁，返回
			if old&(mutexLocked|mutexStarving) == 0 {
				break
			}
            // 运行到这里说明之前是有锁或者饥饿的状态，应该阻塞当前线程
			// 阻塞并等待被唤醒。新来的线程放到队尾，被唤醒的线程放到队头
			queueLifo := waitStartTime != 0
			if waitStartTime == 0 {
				waitStartTime = runtime_nanotime()
			}
			runtime_SemacquireMutex(&m.sema, queueLifo, 1)
            // 被唤醒后，检查是否应该进入饥饿模式
			starving = starving || runtime_nanotime()-waitStartTime > starvationThresholdNs
			old = m.state
            // 如果是饥饿的，那么应该直接把锁交给这个被唤醒的线程
			if old&mutexStarving != 0 {
				if old&(mutexLocked|mutexWoken) != 0 || old>>mutexWaiterShift == 0 {
					throw("sync: inconsistent mutex state")
				}
                // 设置为加锁，并且waiter-1
				delta := int32(mutexLocked - 1<<mutexWaiterShift)
				if !starving || old>>mutexWaiterShift == 1 {
					// 如果当前线程没有饥饿，或者这个是最后一个waiter，那么退出饥饿模式
					delta -= mutexStarving
				}
                // 通过阻塞唤醒后获取到锁，返回
				atomic.AddInt32(&m.state, delta)
				break
			}
            // 以唤醒的状态重新走一遍流程
			awoke = true
			iter = 0
		} else {
            // CAS失败，重来一遍上述流程
			old = m.state
		}
	}

	if race.Enabled {
		race.Acquire(unsafe.Pointer(m))
	}
}
```

Unlock方法：

```go
func (m *Mutex) Unlock() {
	if race.Enabled {
		_ = m.state
		race.Release(unsafe.Pointer(m))
	}

	// 因为当前线程持有锁，所有直接state-mutexLocked就行
	new := atomic.AddInt32(&m.state, -mutexLocked)
	if new != 0 {
        // new!=0说明可能有其他线程在等待，还要进一步处理
		m.unlockSlow(new)
	}
}
```

unlockSlow方法：

```go
func (m *Mutex) unlockSlow(new int32) {
	if (new+mutexLocked)&mutexLocked == 0 {
		fatal("sync: unlock of unlocked mutex")
	}
	if new&mutexStarving == 0 {
        // 如果不饥饿，还要进一步判断需不需要唤醒waiter
		old := new
		for {
            // 如果等待队列为空，或者锁已经被获取，或者有醒着的线程去尝试获取锁，或者线程处于饥饿
            // 直接返回即可，因为上述情况都不需要这个线程去唤醒waiter
			if old>>mutexWaiterShift == 0 || old&(mutexLocked|mutexWoken|mutexStarving) != 0 {
				return
			}
            // 唤醒一个waiter
			new = (old - 1<<mutexWaiterShift) | mutexWoken
			if atomic.CompareAndSwapInt32(&m.state, old, new) {
				runtime_Semrelease(&m.sema, false, 1)
				return
			}
			old = m.state
		}
	} else {
        // 如果饥饿（说明有饥饿的waiter），那么一定需要由该线程赖唤醒waiter，让他去走获取锁的流程
        // 注意此时mutexLocked=0，但由于mutexStarving=1，所以新来的线程不会去获取到锁
		runtime_Semrelease(&m.sema, true, 1)
	}
}
```

## sync.RWMutex

`sync.RWMutex`综合了写优先和非写优先锁：

1. 避免writer饿死：当writer来尝试获取锁的时候，在其后面来的reader都得阻塞。写优先核心是通过`readerCount`这个原子变量实现的
2. 避免reader饿死：当writer释放锁时，在等待的reader马上获取到锁，即使当前还有其他的writer在等待。然后writer才能去竞争锁，继而阻塞后来的reader

RWMutex结构体：

```go
type RWMutex struct {
	w           Mutex        // writer互斥
	writerSem   uint32       // 写阻塞队列
	readerSem   uint32       // 读阻塞队列
	readerCount atomic.Int32 // 所有reader的数量，负数表示有writer在等，优先让writer先获取锁
	readerWait  atomic.Int32 // 在等待写锁释放的reader数量
}
```

Lock方法：

```go
func (rw *RWMutex) Lock() {
	...
	// 与其他writer互斥
	rw.w.Lock()
	// 通知其他reader现在有writer，禁止新的reader获取锁
	r := rw.readerCount.Add(-rwmutexMaxReaders) + rwmutexMaxReaders
	// 阻塞等待当前已经获取锁的读者完成
	if r != 0 && rw.readerWait.Add(r) != 0 {
		runtime_SemacquireRWMutex(&rw.writerSem, false, 0)
	}
    ...
}
```

Unlock方法：

```go
func (rw *RWMutex) Unlock() {
	...
	// 通知其他reader现在没有writer
	r := rw.readerCount.Add(rwmutexMaxReaders)
	if r >= rwmutexMaxReaders {
		race.Enable()
		fatal("sync: Unlock of unlocked RWMutex")
	}
	// 唤醒所有reader
	for i := 0; i < int(r); i++ {
		runtime_Semrelease(&rw.readerSem, false, 0)
	}
    // 唤醒其他的writer
	rw.w.Unlock()
    ...
}
```

RLock方法：

```go
func (rw *RWMutex) RLock() {
	...
    // 加入一个reader
	if rw.readerCount.Add(1) < 0 {
        // 现在有writer，reader直接阻塞不去与writer竞争
		runtime_SemacquireRWMutexR(&rw.readerSem, false, 0)
	}
    ...
}
```

RUnlock方法：

```go
func (rw *RWMutex) RUnlock() {
	...
    // 减少一个reader
	if r := rw.readerCount.Add(-1); r < 0 {
        // readerCount<0，说明有writer在等待，要进一步唤醒writer
		rw.rUnlockSlow(r)
	}
	...
}
```

rUnlockSlow方法：

```go
func (rw *RWMutex) rUnlockSlow(r int32) {
	...
    // 最后一个释放锁的reader负责唤醒writer
	if rw.readerWait.Add(-1) == 0 {
		runtime_Semrelease(&rw.writerSem, false, 1)
	}
}
```

## sync.Map

`sync.Map`是线程安全版本的原生`map`

```go
type Map struct {
	mu Mutex
	read atomic.Pointer[readOnly]
	dirty map[any]*entry
	misses int
}
```



## 参考

[Recursive locking in Go](https://stackoverflow.com/questions/14670979/recursive-locking-in-go)

[What is an invariant?](https://stackoverflow.com/questions/112064/what-is-an-invariant)

[Russ's discussion about reentrant lock](https://groups.google.com/g/golang-nuts/c/XqW1qcuZgKg/m/Ui3nQkeLV80J)