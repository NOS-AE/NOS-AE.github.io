---
title: "Kubernetes 存储入门：从 PV、PVC 到 CSI 与云硬盘"
date: 2026-10-06T00:00:00+08:00
description: "用 YAML 认识 Kubernetes 持久化存储，以 ESSD 和 CSI Hostpath 解释组件协作、部署方式与数据存放位置。"
tags: [k8s, storage, csi]
categories: [k8s]
draft: false
---

容器的可写层适合临时数据；当 Pod 被删除并重新创建时，新 Pod 不会自动继承旧 Pod 可写层中的文件。数据库文件、上传内容等需要更长的生命周期。Kubernetes 通过 Volume 把存储挂到容器中，其中 `emptyDir` 随 Pod 生命周期消失（对于容器级重启则不会消失）；需要独立于 Pod 存活的数据，通常使用 PersistentVolume（PV）和 PersistentVolumeClaim（PVC）。

[toc]

## 为什么需要 PV/PVC

先想象一个没有 PV/PVC 的世界。如果直接在 Pod 里写：

```yaml
volumes:
  - name: data
    nfs:
      server: 192.168.1.100
      path: /data
```

这样看起来就带来一系列耦合问题：

- 应用开发者需要知道底层存储的具体细节（是 NFS 还是 Ceph？服务器地址是多少？）
- 存储变更意味着要改 Pod 定义，应用和基础设施强耦合

Kubernetes 的解法是经典的 **关注点分离**：把「存储怎么提供」和「存储怎么使用」拆成两个对象。

- **PV（PersistentVolume）**：由集群管理员或动态供给器提供的 **存储资源**，描述“我这里有一块什么样的存储”。
- **PVC（PersistentVolumeClaim）**：由应用开发者提出的 **存储申请**，描述“我需要一块什么样的存储”。

从资源的角度看，PV 和 PVC 的关系就像是 Node 和 Pod 的关系：Node 有 CPU、内存等资源，而 Pod 则申请并占用一定的 CPU、内存资源。而存储比较特殊，因为它的形式可以是多种多样的，可能是内存、节点上的硬盘，也可以是其他机柜中的云硬盘等。因此 PV/PVC 将存储资源抽象出来，并被 Pod 所使用。

### PV（persistent volume）

PV 是集群级别的资源（不隶属于任何 namespace），它是对底层存储的一层抽象。一个 PV 的关键字段：

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-nfs-10g
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: standard
  nfs:
    server: 192.168.1.100
    path: /data/pv10g
```

### PVC（persistent volume claim）

PVC 隶属于某个 namespace，是开发者直接打交道的东西。它描述需求，而不是具体实现：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: myapp-data
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
  storageClassName: standard
```

由于 PV 和 PVC 是一对一关系，每次创建新的 PVC，都要创建对应的 PV。因此这里的 PVC 例子中并没有直接引用 PV，而是引用了 storageclass 这个东西，它用于按模板自动创建出 PV，下面会详细介绍。

然后 Pod 里这样引用：

```yaml
spec:
  containers:
    - name: myapp
      image: myapp:1.0
      volumeMounts:
        - mountPath: /var/lib/data
          name: data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: myapp-data
```

看到没？Pod 无需感知底层是 NFS 还是 Ceph，它只认 PVC。

### StorageClass

一句话定义：**StorageClass 是描述「某一类存储」的模板，包含由哪个 provisioner 供给、用什么参数、什么回收策略等信息。**

假设你是一个集群管理员，有 50 个应用需要存储。手工方式是这样：

1. 先建 50 个 PV，每个都要写底层存储的细节（NFS 地址、Ceph 配置、AWS EBS 参数……）
2. 开发者再建 50 个 PVC 去匹配

问题很明显：

- **管理员成了瓶颈**，每个新需求都要人工介入

- **规格难预测**，建多了浪费，建少了 PVC 一直 Pending

- **存储类型混杂**，SSD、HDD、不同后端，全靠命名约定管理

因此，StorageClass 有点像 java、c++ 等编程语言里 "类" 的概念，而 PVC 则是使用这个类具体创建出来的对象，即：**把「如何创建一块存储」这件事模板化**。管理员只定义一次模板，之后 PVC 一来，系统自动按模板创建 PV。

一个 StorageClass 示例：

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fast-ssd
provisioner: kubernetes.io/aws-ebs
parameters:
  type: gp3
  fsType: ext4
reclaimPolicy: Delete
allowVolumeExpansion: true
volumeBindingMode: WaitForFirstConsumer
```

它是 **集群级资源**（不属于任何 namespace），因为存储本身是集群共享的基础设施。

通过 storageclass 给 pod 绑定 pvc 的流程：

1. 开发者创建 PVC，指定 storageClassName: fast-ssd
2. 控制器发现没有现成 PV 可绑定
3. 找到名为 fast-ssd 的 StorageClass
4. 调用其 provisioner，传入 parameters
5. provisioner 在底层创建真实存储，并生成对应 PV 对象
6. PV 与 PVC 自动绑定，状态变为 Bound
7. Pod 挂载 PVC，开始使用

一般云厂商管理的 K8S 集群中，会默认定义了几种推荐使用的 storageclass，并提供完善的文档用来描述这些 storageclass（如 [阿里云 ACK](https://help.aliyun.com/zh/cs/user-guide/use-cloud-disk-dynamic-storage-volumes)）。

#### volumeBindingMode

- Immediate（默认）：PVC 一创建就立刻绑定 PV。听起来合理，但在多云/多可用区场景会出问题：PVC 创建时绑定了一个 `us-east-1a` 的 EBS 卷，结果 Pod 被调度到了 `us-east-1b`，跨区挂载直接失败。
- WaitForFirstConsumer：等第一个使用该 PVC 的 Pod 被调度时，才绑定 PV。这样 provisioner 能根据 Pod 的调度位置，在同一个可用区创建存储。生产环境一般用这个模式。

## CSI

写完 StorageClass，你可能会有一个疑问：provisioner 到底是谁？它凭什么能创建出 PV？这个问题的答案，就是 **CSI（Container Storage Interface）**。

在 CSI 出现之前，Kubernetes 的存储插件是 **in-tree** 的。什么意思？就是存储驱动的代码直接写在 Kubernetes 核心代码库里，和 kubelet、kube-controller-manager 一起编译、一起发布。这带来的一系列耦合问题不言而喻。

CSI 的核心目标非常清晰：**定义一个标准接口，让存储厂商的驱动完全脱离 Kubernetes 核心，独立开发、独立部署、独立发布**。它借鉴了容器运行时接口（CRI）和容器网络接口（CNI）的思路——把“编排系统”和“底层实现”用标准协议隔开。

CSI 规范定义了三组 gRPC 服务：

- **Controller**：负责卷的生命周期管理，比如创建、删除、快照、扩容
- **Node**：负责在具体节点上把卷挂载/卸载到容器里
- **Identity**：提供驱动的元信息，比如名字、版本、能力

存储厂商只需要实现这三组接口，发布一个 driver 二进制，不需要碰 Kubernetes 一行代码。

### 部署形态

CSI 驱动在 Kubernetes 里不是单独一个组件，而是被拆成了 **Controller 侧** 和 **Node 侧** 两部分，分别部署。

Controller 组件通常以 Deployment 或 StatefulSet 部署，单副本，跑在控制平面或任意节点上。它里面有两个容器：

- [external-provisioner](https://github.com/kubernetes-csi/external-provisioner)（sidecar）：监听 PVC 的创建/删除事件，调用 CSI 驱动的 `CreateVolume`/`DeleteVolume`
- [external-attacher](https://github.com/kubernetes-csi/external-attacher)（sidecar）：监听 VolumeAttachment 对象，调用 `ControllerPublish`/`ControllerUnpublish`

这两个 sidecar 通过 Unix Socket 和同一个 Pod 里的 CSI driver 容器通信。

Node 组件以 DaemonSet 部署，每个节点跑一个 Pod：

- [node-driver-registrar](https://github.com/kubernetes-csi/node-driver-registrar)（sidecar）：把 CSI driver 注册到 kubelet
- **CSI driver**：接收 kubelet 的调用，执行 `NodePublishVolume`（挂载到容器路径）和 `NodeUnpublishVolume`（卸载）

整个调用链是这样的：

```
PVC 创建
   ↓
external-provisioner 监听到
   ↓ 通过 gRPC
CSI driver 的 CreateVolume
   ↓
底层存储创建卷 → 生成 PV
   ↓
Pod 调度到节点
   ↓
AttachDetachController 发现 PV 是 CSI 类型
   ↓
创建 VolumeAttachment 对象(spec.attacher = csi 驱动名, spec.nodeName = node-a)
   ↓
external-attacher 监听到这个对象
   ↓
调用 CSI ControllerPublishVolume
   ↓
底层存储把卷 attach 到 node-a
   ↓
external-attacher 更新 status.attached = true
   ↓
AttachDetachController 确认 attach 完成
   ↓
kubelet 执行 NodePublishVolume 挂载到容器
```

现在可以回答开头的问题了。当你写：

```yaml
provisioner: ebs.csi.aws.com
```

这个 `ebs.csi.aws.com` 就是 CSI 驱动的名字。当 PVC 创建时，external-provisioner 会去调用对应的 CSI 驱动，驱动最终会 AWS 上创建一块 EBS 卷，并生成 PV 对象。

### CSI 带来的新能力

CSI 不只是把老功能搬了个家，它还带来以下新功能：

- **卷快照。** 在 CSI 之前，Kubernetes 没有统一的快照 API。CSI 引入了 `VolumeSnapshot` 和 `VolumeSnapshotClass`，让快照成为一等公民。
- **在线扩容。** 不用停 Pod 就能扩大卷容量，前提是 StorageClass 开了 `allowVolumeExpansion` 且驱动支持。
- **卷克隆。** 从一个已有 PVC 直接创建一个新 PVC，用于快速复制数据。
- **更精细的拓扑感知。** 驱动可以告诉调度器卷在哪个可用区，配合 `WaitForFirstConsumer` 做更聪明的调度。

这些新特性只加在 CSI 接口上，in-tree 插件不再获得新功能。这也是为什么官方一直在推动 CSI 迁移。

### 不使用 CSI 的场景

CSI 讲完了标准接口，但有一个现实问题：**如果你跑的是单机集群、边缘环境，或者根本没接云存储，CSI 驱动从哪来？**

不是所有场景都需要 Ceph、AWS EBS 这种重量级方案。有时候你只是想让 K3s 单节点、开发测试集群、或者边缘设备上的 Pod，能有一个能动态供给的持久卷。[Local Path Provisioner](https://github.com/rancher/local-path-provisioner) 就可以用来干这种事情。

Kubernetes 原生有两种使用本地存储的方式：`hostPath` 和 `local` volume。但它们都有明显的短板：

- hostPath：绑定节点路径，Pod 换节点数据就丢了，而且不支持动态供给
- local：虽然支持持久化，但需要管理员 **手工创建 PV**，然后用 PVC 去静态匹配，用起来很麻烦

Local Path Provisioner 本身是一个 **provisioner**——也就是 StorageClass 里 `provisioner` 字段指向的那个东西。但它和 CSI 驱动不太一样，它不实现完整的 CSI gRPC 接口，而是一个更轻量的 Kubernetes 原生控制器。

核心逻辑很朴素：

1. 监听 PVC 的创建事件
2. 在目标节点上 **创建一个目录**（默认在 `/opt/local-path-provisioner/`）
3. 把这个目录包装成一个 PV 对象
4. 绑定 PVC 和 PV

当 Pod 调度到某个节点时，provisioner 会该节点创建对应的目录。默认情况下，它选择卷目录的规则是：Pod 被调度到哪个节点，就在哪个节点上创建存储。

因此简单来说，local path 不是 CSI 驱动，但能完成类似动态供给的工作。需要注意的是，local path 生成的 PV 还定义了节点亲和性，一旦这个节点上的 Pod 被重调度到其它节点时，由于这个 Pod 引用的 PVC 所绑定的 PV 仍然在旧节点上，所以新 Pod 会起不来，查看 Pod 详情时会看到类似这样的信息：

```
Warning  FailedScheduling  0/2 nodes are available:
  1 node(s) had volume node affinity conflict.
```

通过 local path 的例子我们可以发现 PV、PVC、storageclass 等 k8s 资源类型与 CSI 并无直接关系。k8s 资源对象只是用于描述期望的状态，而 CSI 与 local-path 则属于具体的实现方式。

另外我们注意到，在 local-path 的例子中并没有出现过 VolumeAttachment 的身影，这是因为 `VolumeAttachment` 是为 CSI 这种 **需要独立 attach/detach 操作** 的存储设计的。本地存储（local volume、hostPath）不需要“attach”这个动作——卷就在节点本地，没有“附着到节点”这一步。这也解释了为什么 Local Path Provisioner 的绑定约束是靠 PV 的 `nodeAffinity` 实现的，而不是靠 VolumeAttachment。两者的机制完全不同。

### 云厂商 CSI 驱动示例

以阿里云为例，介绍云硬盘 ESSD 如何挂载到 ACK 集群的 Pod 中。

![阿里云 ESSD 经 CSI 创建、连接到 ECS 并挂载到容器的组件协作流程](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/csi-volume-workflow.svg)

#### 第一步：创建 ESSD

用户提交 PVC 和引用它的 Pod。调度器为 Pod 选择合适节点；`external-provisioner` 观察到待供盘的 PVC，向阿里云 CSI 的 Controller 服务调用 `CreateVolume`。驱动调用阿里云 API 创建云盘，并返回云盘 ID。随后 `external-provisioner` 创建 PV，PVC 与 PV 绑定。

此时「盘存在」不等于「Pod 已能访问」。PV 的 `spec.csi.driver` 指明使用哪个驱动，`spec.csi.volumeHandle` 通常保存真实云盘的 ID，例如 `d-...`。

#### 第二步：将 ESSD 接到 ECS

Kubernetes 的 attach/detach controller 发现：Pod 要运行在某台 ECS 上，而这台 ECS 需要对应的卷。它创建 `VolumeAttachment`，记录「哪个 PV 应接到哪个节点」。

`external-attacher` 观察到这个对象，调用驱动的 `ControllerPublishVolume`；阿里云 CSI 驱动再调用阿里云 `AttachDisk` API。接盘完成后，`external-attacher` 将 `VolumeAttachment.status.attached` 更新为 `true`。

因此，`VolumeAttachment` 是 Kubernetes 保存接盘意图和结果的对象，不是 CSI 协议的一部分；`ATTACHED=true` 只说明接到了节点，还不能证明 Pod 内的目录已挂好。

#### 第三步：在 ECS 上准备并挂载

目标 ECS 上的 kubelet 看到了分配给自己的 Pod，读取它引用的 PVC/PV，知道该 Pod 需要这个 CSI 卷。节点驱动预先向 kubelet 注册了通信 socket，kubelet 因而知道要调用哪个驱动。接盘完成后，kubelet 的卷管理流程开始准备本机挂载：

1. 若驱动支持暂存阶段，kubelet 调用 `NodeStageVolume`。节点驱动找到块设备，必要时为全新空盘创建文件系统，再挂到节点的暂存路径。
2. kubelet 调用 `NodePublishVolume`。节点驱动把卷提供到该 Pod 对应的目标路径，容器最终在 `/data` 看到它。

`NodeStageVolume` 是可选阶段，不是所有 CSI 驱动都会执行；`NodePublishVolume` 才是让卷出现在工作负载路径的关键调用。

Pod 开始读写后，普通文件 I/O 走的是 Linux 文件系统、ECS 的块设备和 ESSD。每次 `read`/`write` 不会再经过 sidecar 或 CSI RPC；这些组件管理卷的生命周期，并不转发应用的数据。

把三个阶段压缩成一行，就是：

```text
PVC → CreateVolume（建 ESSD）
Pod 选定节点 → ControllerPublishVolume（ESSD 接到 ECS）
ECS 上的 kubelet → NodeStage/NodePublish（挂载给 Pod）
```

#### 如何观察这条链路？

在已安装对应 CSI 驱动的集群中，可以依次查看申请、实际卷、接盘记录和 Pod：

```sh
kubectl get pvc
kubectl get pv
kubectl get volumeattachments
kubectl describe pod app
```

PVC 为 `Bound` 说明它已经绑定到 PV；PV 的 `spec.csi` 能看到驱动名和卷 ID；`VolumeAttachment` 的 `ATTACHED=true` 表示卷已接到节点；Pod 正常运行并可访问 `/data`，才说明节点挂载也完成了。并非所有 CSI 卷都需要 `VolumeAttachment`，例如驱动声明无需控制器接盘时，就没有这一步。

贯穿这篇文章的关键问题是：**数据实际存放在哪里？谁负责创建卷？卷如何到达 Pod 所在节点并出现在容器内？** 对 ESSD 来说，答案分别是云盘、CSI 控制器驱动，以及「云端接盘 + 节点挂载」；对 local-path 来说，答案则是节点目录、本地供盘者，以及节点上的目录挂载。

## 参考

[PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)

[CSI 驱动部署说明](https://kubernetes-csi.github.io/docs/deploying.html)

[阿里云驱动实现](https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver/blob/master/pkg/disk/controllerserver.go)
