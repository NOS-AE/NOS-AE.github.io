---
date: 2025-12-05T23:45:16+08:00
title: K8S的Service
tags: [k8s]
categories: [k8s]
draft: false
---

## 虚拟 IP 和服务代理

每个 k8s 节点上都运行了一个 kube-proxy，kube-proxy 以 pod 形式存在。

kube-proxy 与 [Service](https://kubernetes.io/docs/concepts/services-networking/service) 息息相关，可以说 Service 负责定义规则，而 kube-proxy 则在节点上具体实现由 Service 指定的规则。具体地，kube-proxy 监听 Service 和 EndpointSlice 资源的更新，并配置节点上相应的路由规则。

![img](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/services-iptables-overview-20251206173122420.svg)

有人会问，为什么不用 DNS，配置多条 A 记录，结合 round-robin 等负载均衡算法来自动将虚拟 IP 的请求分发到真实 IP 上。原因是：

- DNS 很可能不遵守 A 记录的 TTLs（Time to live），导致真实 IP 发生变化时不能及时感知；
- 有些 APP 可能只访问 DNS 一次，但 DNS 可能会将这条记录永久保存；
- 即使 DNS 遵守 TTLs 配置，然后为了及时感知变化，TTLs 配置得很低的话，可能导致 DNS 负载变得很高。

### kube-proxy 的代理模式

kube-proxy 不做实际的包转发，而是根据从 apiserver 监听 Service 与 EndpointSlice 的变化，并维护节点上的包处理规则，比如配置 iptables 规则、ipvs 规则等，最终由内核来完成真实流量的转发。

根据不同的配置方式，分为几种代理模式（linux）：

- iptables
- ipvs
- nftables

下面以 iptables 模式的 kube-proxy + clusterIP Service 为例，说明从 Service 创建到一个请求打到 pod 上是如何发生的。

首先创建 Service，控制面为其分配了一个虚拟 IP 10.0.0.1，端口为 1234，此时所有节点上的 kube-proxy 监听到这个 Service 的创建。

随后每个节点的 kube-proxy 会在节点上创建相同的 iptables 规则，主要分为三条 iptables 链（在节点上使用 `iptables-save -t nat` 可以查看）：

1. KUBE-SERVICES 链：匹配 Service 的虚拟 IP 端口

   ``` bash
   -A KUBE-SERVICES -d 10.96.0.123/32 -p tcp --dport 80 -j KUBE-SVC-XXXX
   ```

2. KUBE-SVC-xxx 链：包含多个后端 pod 后端链，进入该链的流量，会被随机或轮询转发到其中一个 Pod 的 KUBE-SEP 链

   ``` bash
   -A KUBE-SVC-XXXX -j KUBE-SEP-aaa # Pod A
   -A KUBE-SVC-XXXX -j KUBE-SEP-bbb # Pod B
   ...
   ```

3. KUBE-SEP-yyy 链：通过 DNAT 将请求的目标地址从虚拟 IP: port 改写为 podIP: targetPort

   ``` bash
   -A KUBE-SEP-aaa -j DNAT --to-destination 10.244.1.10:8080
   ```

通过维护这样的 iptables 分层模型，可以快速计算出到最终的目标 ip: port 的路由，整体过程如下：

```
PREROUTING 入口
    ↓
KUBE-SERVICES: 判断目标是不是某个 Service
    ↓
KUBE-SVC-xxxx: 对应该 Service 的 LB
    ↓
KUBE-SEP-yyyy: 选到具体某个 pod
    ↓
DNAT → PodIP:port
```

kube-proxy 的负载均衡，或者说 Service 的负载均衡的原理是配置 KUBE-SVC-XXXX 链，比如随机负载均衡：

``` bash
-A KUBE-SVC-ABCDEF12345 -m statistic --mode random --probability 0.50 -j KUBE-SEP-111111
-A KUBE-SVC-ABCDEF12345 -j KUBE-SEP-222222
```

表示有两个 pod，按 50% 概率选择第一个 Pod，剩下的流量去第二个 Pod。

最后扩展一下，跨节点路由是怎么实现的？由 CNI(container network interface)插件来处理，比如 Flannel 首先会写系统路由表：

``` bash
# 当前有两个节点
# node1 → 10.244.1.0/24
# node2 → 10.244.2.0/24

# 在node1
route add 10.244.2.0/24 via 192.168.1.102 dev flannel.1

# 在node2
route add 10.244.1.0/24 via 192.168.1.101 dev flannel.1
```

然后通过建立 vxlan 隧道，将数据封装在 UDP 包中发送到对端。

说白了就是，cni 负责 route、kube-proxy 负责 netfilter，前者决定包往哪儿走（L3，往哪个网卡/哪个节点发），后者决定包怎么被处理（L3/L4，是否做 DNAT/SNAT？是否丢弃？是否转发到别的 Pod）

## Service

Service 用于提供统一稳定的 ip，并将流量负载均衡到 Service 所对应的一组 Endpoints。

结合上一节介绍的虚拟 IP 与 kube-proxy，用一个最简单的 ClusterIP 类型的 Service 来入门：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
spec:
  selector:
    app.kubernetes.io/name: MyApp
  ports:
    - protocol: TCP
      port: 80
      targetPort: 9376
```

首先控制面给这个 Service 分配一个集群 IP（虚拟 IP），Service 控制器会不断检查是否有与 selector 匹配的 pods，并更新到 EndpointSlice 中。最后就是根据上面所说的，kube-proxy 检测到 Service 与 EndpointSlice 的更新，配置节点的 iptables。

带 selector 的 Service 的 backend 一般就是 Pod，但如果 backend 不是 Pod，可能是集群外部的服务，那么可以使用不带 selector 的 Service，没有 selector 的 Service 不会自动创建 EndpointSlice，由自己来配置并创建 EndpointSlice：

``` yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
spec:
  ports:
    - name: http
      protocol: TCP
      port: 80
      targetPort: 9376
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: my-service-1 # EndpointSlice命令的最佳实践是以svc的名称作为前缀
  labels:
    # 这个label的值必须是svc的名称，将EndpointSlice与svc关联起来
    kubernetes.io/service-name: my-service
addressType: IPv4
ports:
  - name: http # should match with the name of the service port defined above
    appProtocol: http
    protocol: TCP
    port: 9376
endpoints:
	# 注意endpoint不能是虚拟IP
  - addresses:
      - "10.4.5.6"
  - addresses:
      - "10.1.2.3"
```

Service 支持多个端口到目标端口的映射：

``` yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
spec:
  selector:
    app.kubernetes.io/name: MyApp
  ports:
    - name: http
      protocol: TCP
      port: 80
      targetPort: 9376
    - name: https
      protocol: TCP
      port: 443
      targetPort: 9377
```

### Service 的类型

之前我们默认使用的都是 ClusterIP 类型的 Service，实际上 Service 一共有以下几种类型：

- ClusterIP：为 Service 分配一个仅在集群内可访问的虚拟 IP，如果要暴露到公网需要在接一个 Ingress 或者 Gateway

  ``` yaml
  apiVersion: v1
  kind: Service
  metadata:
    name: service-clusterip
  spec:
    selector:
      app.kubernetes.io/name: MyApp
    ports:
      - protocol: TCP
        port: 80
        targetPort: 8080
  ```

  结果：

  ``` bash
  $ kubectl get svc service-clusterip
  NAME                TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)   AGE
  service-clusterip   ClusterIP   10.96.229.241   <none>        80/TCP    2m28s
  ```

- NodePort：在 ClusterIP 的基础上，为每个 node 开放一个静态端口

  ``` yaml
  apiVersion: v1
  kind: Service
  metadata:
    name: service-nodeport
  spec:
    type: NodePort
    selector:
      app.kubernetes.io/name: MyApp-nodeport
    ports:
      - port: 80
        targetPort: 80
        nodePort: 30007 # nodePort也可以由控制面动态分配
  ```

  结果：

  ``` bash
  $ kubectl get svc service-nodeport
  NAME               TYPE       CLUSTER-IP    EXTERNAL-IP   PORT(S)        AGE
  service-nodeport   NodePort   10.96.31.20   <none>        80:30007/TCP   2m46s
  ```

- LoadBalancer：在 Nodeport 的基础上，自动创建一个云厂商提供的负载均衡器

- ExternalName：最特殊的类型，没有 backend，不涉及代理和转发，仅仅是将请求重定向到一个外部的 DNS 名称。比如下面的配置，当访问 `my-service.prod.svc.cluster.local` 名称时，会返回一个 CNAME 记录，值为 `my.database.example.com`

  ``` yaml
  apiVersion: v1
  kind: Service
  metadata:
    name: my-service
    namespace: prod
  spec:
    type: ExternalName
    externalName: my.database.example.com
  ```

## 无头 Service

普通的 Service 会分配一个 ClusterIP，并负载均衡所有背后的 Pod，旨在将多个无状态 backend 抽象为统一的入口。但无头 Service，不会分配 ClusterIP、kube-proxy 也不会处理（不生成任何 iptables/IPVS 规则、不做负载均衡），旨在将每个 Pod 的 IP 直接暴露出去，让应用层来决定如何连接。

创建无头 Service 只需要将 ClusterIP 类型的 Service 的 `.spec.clusterIP` 指定为 `None`：

``` yaml
spec:
  clusterIP: None
```

**无头 Service 暴露的 Pod IP 方式是集群 DNS。**

其实对于普通 Service 来说，也会涉及到 DNS，DNS 会创建一个 Service 的完全限定域名（FQDN，Fully Qualified Domain Name），命令为 `<service-name>.<namespace>.svc.cluster.local`，其有一条值为 ClusterIP 的 A 记录。

无头 Service 的完全限定域名下则包含了 n 条 A 记录（ n 为 backend Pod 的数量），值分别为每个 Pod 的 IP，并且还会为每个 Pod 创建单独完全限定域名，命名为 `<pod-name>.<service-name>.<namespace>.svc.cluster.local`，每个域名下包含值为该 Pod 的 IP 的 A 记录。 

因此，无头 Service 的核心价值在于它提供了两种粒度的 DNS 解析：Service 域名（用于发现）和 Pod 域名（用于身份识别），前者可以用来做客户端侧负载均衡，后者用来为每个 Pod 提供稳定的身份，用于在有状态服务中 Pod 之间互相识别对等节点以稳定协同工作，即使 Pod 重建了，只要 Pod 名称没变，域名也不会变。举个 MySQL 主从节点的例子：

| **环节**      | **动作描述**                                                 | **无头 Service 的作用**                                      |
| ------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| 集群初始化    | `mysql-1` 需要知道主节点 `mysql-0` 的 **稳定地址** 来进行复制配置。 | `mysql-1` 通过解析 `mysql-0.mysql-headless.default.svc.cluster.local` 稳定地找到主节点的 IP，建立复制连接。 |
| 内部访问      | 应用客户端需要读写数据，并将连接分散到主从节点。             | 客户端可以查询 `mysql-headless`，获取所有 Pod IP 列表，然后根据自身的逻辑（例如，写操作连主节点 IP，读操作连从节点 IP）进行客户端侧的连接管理。 |
| 节点故障/重启 | `mysql-0` Pod 发生故障，被 K8s 重新调度。                    | 即使 Pod IP 变了，它的稳定域名 `mysql-0.mysql-headless` 仍然解析到新的 IP 地址，复制关系和配置无需手动修改。 |

## DNS

关于上面提到的集群 DNS，更详细的内容参考 <https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/>
