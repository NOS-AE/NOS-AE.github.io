---
title: "Kubernetes 存储入门：从 PV、PVC 到 CSI 与云硬盘"
date: 2026-10-06T00:00:00+08:00
description: "用一组 YAML 认识 Kubernetes 持久化存储，再以阿里云 ESSD 说明 CSI 的工作过程。"
tags: [k8s, storage, csi]
categories: [k8s]
draft: false
---

容器的可写层适合临时数据；当 Pod 被删除并重新创建时，新 Pod 不会自动继承旧 Pod 可写层中的文件。数据库文件、上传内容等需要更长的生命周期。Kubernetes 通过 Volume 把存储挂到容器中，其中 `emptyDir` 随 Pod 生命周期消失；需要独立于 Pod 存活的数据，通常使用 PersistentVolume（PV）和 PersistentVolumeClaim（PVC）。

## 从一组 YAML 认识存储资源

可以把 PVC 看成「存储申请」，PV 看成「实际分配到的卷」，StorageClass 看成「由谁按什么规则提供卷」。Pod 只引用 PVC，不必知道底层是一块云硬盘、网络文件系统，还是某台节点上的目录。

下面以阿里云 ACK 集群中的 ESSD 云盘为例。这里的「节点」指运行 Pod 的机器，在 ACK 中通常是一台 ECS。假设集群已安装阿里云云盘 CSI 驱动，先定义 StorageClass；暂时把 `provisioner` 理解成负责提供存储的组件名称：

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: essd-demo
provisioner: diskplugin.csi.alibabacloud.com
parameters:
  type: cloud_essd
  performanceLevel: PL1
  fstype: ext4
reclaimPolicy: Retain
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
```

`provisioner` 指定供盘组件；`type` 选择 ESSD；`reclaimPolicy` 决定 PVC 删除后是否保留底层云盘。`WaitForFirstConsumer` 表示等到有 Pod 使用这份申请、调度器选出合适节点后，再确定云盘的可用区。ACK 默认也提供云盘 StorageClass；这里单独定义一个名字，是为了把关键配置展示完整。[ACK 动态云盘文档](https://www.alibabacloud.com/help/en/ack/ack-managed-and-ack-dedicated/user-guide/use-dynamically-provisioned-disk-volumes)

应用通过 PVC 申请 20Gi 存储：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: app-data
spec:
  storageClassName: essd-demo
  accessModes:
    - ReadWriteOnce
  volumeMode: Filesystem
  resources:
    requests:
      storage: 20Gi
```

`storageClassName` 把申请交给上面的 StorageClass；`resources.requests.storage` 是申请容量；`volumeMode: Filesystem` 表示容器要使用文件系统目录，而不是直接操作裸块设备。`ReadWriteOnce`（RWO）主要限制卷在同一时间可被哪些 **节点** 以读写方式使用，并不等于「只允许一个 Pod」。ACK 默认 ESSD StorageClass 的最小容量为 20Gi，因此示例也使用 20Gi。[Kubernetes PV/PVC 文档](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)

Pod 在 `volumes` 中引用 PVC，再用 `volumeMounts` 指定容器内的路径。Pod 和 PVC 必须处于同一个命名空间：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: app
spec:
  containers:
    - name: app
      image: busybox:1.36
      command: ["sh", "-c", "sleep 36000"]
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: app-data
```

当供盘完成，集群会自动生成 PV 并把它与 PVC 绑定。下面只展示生成的 PV 中最重要的字段，**不是需要手工提交的清单**：

```yaml
kind: PersistentVolume
spec:
  capacity:
    storage: 20Gi
  accessModes:
    - ReadWriteOnce
  storageClassName: essd-demo
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: diskplugin.csi.alibabacloud.com
    volumeHandle: d-xxxxxxxx
```

`csi.driver` 指向处理这个卷的 CSI 驱动；`volumeHandle` 是驱动用来识别卷的 ID，在这个例子里对应阿里云云盘 ID。PV 是集群级资源，PVC 属于命名空间。实际生成的 PV 还会包含名称、绑定关系、拓扑等字段。

于是，从应用视角看，关系是 `Pod → PVC → PV → ESSD`。StorageClass 决定 PV 如何被自动创建。这里的「自动创建」叫 **动态供盘**；它是一种工作方式，本身不要求一定采用 CSI。

## 没有 CSI，存储会怎样？

**没有 CSI，不等于 Kubernetes 不能使用持久化存储。** 手工创建的 PV 可以引用内置卷类型；独立运行的本地目录供盘组件也能监听 PVC，自动准备目录并创建 PV。早期云厂商的卷插件甚至直接编译在 Kubernetes 核心组件中。下文会具体介绍一种名为 local-path 的本地目录供盘组件。

问题出在接入越来越多的存储系统时。假设一种新云硬盘需要调用厂商 API 建盘、按可用区选址、接到指定机器，再由该机器识别设备和挂载文件系统。若每种存储都把这些逻辑放进 Kubernetes 核心代码，驱动的开发、修复和发布就得跟着 Kubernetes 的发布节奏走。若各厂商另写一套监听 PVC、处理重试和状态更新的程序，又会重复实现大量 Kubernetes 对接逻辑。

CSI（Container Storage Interface）解决的是 **编排系统与存储驱动之间的接口边界**。它定义 `CreateVolume`、`ControllerPublishVolume`、`NodePublishVolume` 等调用的语义和参数；厂商实现这些接口，并可独立于 Kubernetes 核心发布驱动。CSI 不只负责最后的挂载，还覆盖建卷、删卷、连接节点、解除连接，以及快照、扩容等可选能力。[Kubernetes 对 CSI 的介绍](https://kubernetes.io/blog/2018/01/introducing-container-storage-interface/)

## local-path 是什么，解决了什么问题？

有些集群会安装 [Local Path Provisioner](https://github.com/rancher/local-path-provisioner)，其 StorageClass 的关键配置类似下面这样：

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-path
provisioner: rancher.io/local-path
volumeBindingMode: WaitForFirstConsumer
```

它监听使用该 StorageClass 的 PVC，在选定节点上准备一个本地目录，并创建以 `hostPath` 或 `local` 为来源的 PV。应用仍通过 PVC 使用存储，因此不需要手工创建每个 PV。它是 **动态供盘组件，但不是 CSI 驱动**。

这种方式适合需要节点本地目录的场景，思路也很直观：`PVC → 节点目录 → Pod 挂载`。数据依附于那个节点的目录。生成的 PV 通常带有节点约束，Pod 需要回到保存数据的节点；该节点不可用时，Pod 可能无法调度，数据也不会自动迁移。PVC 写了 `20Gi` 也不代表这个目录被严格限制为 20Gi；Local Path Provisioner 文档明确说明它目前不支持容量上限。

阿里云 ESSD 则是一块需要云平台创建和连接的块设备。它涉及建盘、可用区、接到 ECS、识别设备和挂载文件系统，仅仅创建节点目录无法完成这些操作。反过来，CSI 也可以管理本地存储：**数据放在哪里** 与 **使用哪套接口管理存储** 是两个问题。

## CSI 定义了什么，Kubernetes 又做了什么？

| 名称 | 属于谁 | 作用 |
| --- | --- | --- |
| PVC、PV、StorageClass、Pod、VolumeAttachment | Kubernetes API | 表达需求、实际卷、供盘方式、工作负载和接盘状态 |
| `CreateVolume`、`ControllerPublishVolume`、`NodeStageVolume`、`NodePublishVolume` | CSI 协议 | 规定调用的语义及参数 |
| `external-provisioner`、`external-attacher` | Kubernetes CSI sidecar | 观察 Kubernetes 对象，调用对应的 CSI 接口，回写结果 |
| 阿里云 CSI 驱动 | 存储实现 | 将 CSI 请求变成阿里云 API 调用或节点上的设备、挂载操作 |

sidecar 并非协议强制要求。驱动也可以自行监听 Kubernetes API；通用 sidecar 的价值在于复用监听、重试、状态更新等逻辑，让驱动集中实现存储行为。sidecar 与同一 Pod 内的驱动通常通过共享的 Unix socket 通信。[CSI sidecar 文档](https://kubernetes-csi.github.io/docs/sidecar-containers.html)

CSI 驱动通常按职责部署为两部分。**Controller 服务** 面向整个集群，负责建盘、删盘以及将云盘接到某个节点；**Node 服务** 部署在需要用盘的每台节点上，负责本机设备和挂载。它们是两组逻辑接口，可以由同一个驱动项目实现，但部署位置不同。[CSI 驱动部署说明](https://kubernetes-csi.github.io/docs/deploying.html)

## 一块 ESSD 怎样进入 Pod？

继续使用上面的 StorageClass、PVC 和 Pod。它们描述了期望状态，但云盘尚需经过「创建」「接到节点」「挂载给 Pod」三个动作。这里说的节点是运行 Pod 的 ECS 实例；其他 CSI 驱动可能连接的是别的存储系统。

![阿里云 ESSD 经 CSI 创建、连接到 ECS 并挂载到容器的组件协作流程](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/csi-volume-workflow.svg)

### 第一步：创建 ESSD

用户提交 PVC 和引用它的 Pod。调度器为 Pod 选择合适节点；`external-provisioner` 观察到待供盘的 PVC，向阿里云 CSI 的 Controller 服务调用 `CreateVolume`。驱动调用阿里云 API 创建云盘，并返回云盘 ID。随后 `external-provisioner` 创建 PV，PVC 与 PV 绑定。

此时「盘存在」不等于「Pod 已能访问」。PV 的 `spec.csi.driver` 指明使用哪个驱动，`spec.csi.volumeHandle` 通常保存真实云盘的 ID，例如 `d-...`。[CSI provisioner 文档](https://kubernetes-csi.github.io/docs/external-provisioner.html)

### 第二步：将 ESSD 接到 ECS

Kubernetes 的 **attach/detach controller** 发现：Pod 要运行在某台 ECS 上，而这台 ECS 需要对应的卷。它创建 `VolumeAttachment`，记录「哪个 PV 应接到哪个节点」。

`external-attacher` 观察到这个对象，调用驱动的 `ControllerPublishVolume`；阿里云 CSI 驱动再调用阿里云 `AttachDisk` API。接盘完成后，`external-attacher` 将 `VolumeAttachment.status.attached` 更新为 `true`。

因此，`VolumeAttachment` 是 Kubernetes 保存 **接盘意图和结果** 的对象，不是 CSI 协议的一部分；`ATTACHED=true` 只说明接到了节点，还不能证明 Pod 内的目录已挂好。[VolumeAttachment API](https://kubernetes.io/docs/reference/kubernetes-api/storage/volume-attachment-v1/)、[阿里云驱动实现](https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver/blob/master/pkg/disk/controllerserver.go)

### 第三步：在 ECS 上准备并挂载

目标 ECS 上的 kubelet 看到了分配给自己的 Pod，读取它引用的 PVC/PV，知道该 Pod 需要这个 CSI 卷。节点驱动预先向 kubelet 注册了通信 socket，kubelet 因而知道要调用哪个驱动。接盘完成后，kubelet 的卷管理流程开始准备本机挂载：

1. 若驱动支持暂存阶段，kubelet 调用 `NodeStageVolume`。节点驱动找到块设备，必要时为全新空盘创建文件系统，再挂到节点的暂存路径。
2. kubelet 调用 `NodePublishVolume`。节点驱动把卷提供到该 Pod 对应的目标路径，容器最终在 `/data` 看到它。

`NodeStageVolume` 是可选阶段，不是所有 CSI 驱动都会执行；`NodePublishVolume` 才是让卷出现在工作负载路径的关键调用。[CSI 节点注册说明](https://kubernetes-csi.github.io/docs/node-driver-registrar.html)、[CSI 接口定义](https://github.com/container-storage-interface/spec/blob/master/csi.proto)

Pod 开始读写后，普通文件 I/O 走的是 Linux 文件系统、ECS 的块设备和 ESSD。**每次 `read`/`write` 不会再经过 sidecar 或 CSI RPC**；这些组件管理卷的生命周期，并不转发应用的数据。

把三个阶段压缩成一行，就是：

```text
PVC → CreateVolume（建 ESSD）
Pod 选定节点 → ControllerPublishVolume（ESSD 接到 ECS）
ECS 上的 kubelet → NodeStage/NodePublish（挂载给 Pod）
```

## 删除 Pod 或 PVC 时发生什么？

删除 Pod 后，kubelet 会解除该 Pod 的挂载；在节点上不再使用该卷时，驱动解除节点暂存挂载。随后，如果不再需要接盘，控制器推动卸载，驱动将 ESSD 从 ECS 分离。**只删 Pod 而保留 PVC，数据通常仍留在 ESSD 上**，后续 Pod 可重新使用它。

删除 PVC 则还要看 PV 的回收策略：`Delete` 会由供盘链路删除底层云盘；`Retain` 会保留 PV 和云盘，后续清理或重新使用需要人工处理，云盘也可能继续计费。云盘的可用区、访问模式和云平台接盘限制同样会影响 Pod 能调度到哪里；不要把 `RWO` 理解为自动获得跨节点共享文件系统。[ACK 动态云盘文档](https://www.alibabacloud.com/help/en/ack/ack-managed-and-ack-dedicated/user-guide/use-dynamically-provisioned-disk-volumes)

## 如何观察这条链路？

在已安装对应 CSI 驱动的集群中，可以依次查看申请、实际卷、接盘记录和 Pod：

```sh
kubectl get pvc
kubectl get pv
kubectl get volumeattachments
kubectl describe pod app
```

PVC 为 `Bound` 说明它已经绑定到 PV；PV 的 `spec.csi` 能看到驱动名和卷 ID；`VolumeAttachment` 的 `ATTACHED=true` 表示卷已接到节点；Pod 正常运行并可访问 `/data`，才说明节点挂载也完成了。并非所有 CSI 卷都需要 `VolumeAttachment`，例如驱动声明无需控制器接盘时，就没有这一步。

贯穿这篇文章的关键问题是：**数据实际存放在哪里？谁负责创建卷？卷如何到达 Pod 所在节点并出现在容器内？** 对 ESSD 来说，答案分别是云盘、CSI 控制器驱动，以及「云端接盘 + 节点挂载」；对 local-path 来说，答案则是节点目录、本地供盘者，以及节点上的目录挂载。
