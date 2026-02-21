---
date: 2025-12-14T00:39:29+08:00
title: K8S的GatewayAPI
tags: [k8s]
categories: [k8s]
draft: false
---

## Ingress 已成为过去

上一节说了 Ingress，k8s 已经停止在这个资源上开发新的特性了，已经是 archived / stable 的一种资源类型。相比 Ingress，官方目前推荐使用 Gateway API 来做流量路由。但 Gateway API 不是简单意义上的 Ingress 平替，而是 Kubernetes 官方规划的、用来逐步替代 Ingress 的下一代流量入口 API。

为什么 Ingress 已经走到了尽头？大致有以下几点原因：

- Ingress 同时承担流量入口、路由规则、TLS、Controller 选择、厂商扩展等功能，结果是 annotation 爆炸、不同 Controller 行为不一致等
- Ingress 是单一资源，无法拆分职责，比如由平台团队控制入口（端口 / LB），由业务团队只配路由
- 主要还是因为 Ingress 这个资源本身的可扩展性/结构性问题，所有高级能力只能塞进 annotation，同一个 annotation 在不同 Controller 中语义不同

## Gateway API 的设计

Gateway API 包含四种资源类型，分别是 GatewayClass、Gateway、HTTPRoute、GRPCRoute。

### GatewayClass

类似 IngressClass，是 controller 与 gateway 之间的桥梁。GatewayClass 中通过 `controllerName` 指定了一个 gateway 控制器：

``` yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: example-class
spec:
  controllerName: example.com/gateway-controller
```

### Gateway

定义流量入口端点。Gateway 通过绑定一个 GatewayClass 来指定 controller，然后通过 HTTPRoute / GRPCRoute 将不同的路由规则及其 backend 绑定到 Gateway 上。举个例子，下面的 Gateway 在 80 端口监听 HTTP 请求（address/hostname 没有指定，由 controller 分配）：

``` yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: example-gateway
spec:
  gatewayClassName: example-class
  listeners:
  - name: http
    protocol: HTTP
    port: 80
```

### HTTPRoute

定义 HTTP 请求的路由规则以及 backend

``` yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example-httproute
spec:
  parentRefs:
  - name: example-gateway
  hostnames:
  - "www.example.com"
  rules:
  - matches:
    - path:
        type: PathPrefix
        value: /login
    backendRefs:
    - name: example-svc
      port: 8080
```

这个小例子也很好理解，就是 `Host` 头是指定域名并且请求路径是以 `/login` 为前缀的 HTTP 请求，将会被路由到 `example-svc` 这个 Service 的 8080 端口。

### GRPCRoute

类似 HTTPRoute，GRPCRoute 针对 gRPC 请求定义路由。使用 GRPCRoute 的 Gateway 需要直接支持 HTTP/2 协议。

``` yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GRPCRoute
metadata:
  name: example-grpcroute
spec:
  parentRefs:
  - name: example-gateway
  hostnames:
  - "svc.example.com"
  rules:
  - backendRefs:
    - name: example-svc
      port: 50051
```

上面这个小例子也很简单，所有 `host` 是指定域名的 gRPC 请求都会路由到 `example-svc` 这个 Service 的 50051 端口。

类似 HTTPRoute 的 path 路由规则，GRPCRoute 还支持 gRPC 的语义，比如下面例子中，除了 `host` 外，还可以指定匹配 service 和 method 才路由到 Service 的 50051：

``` yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GRPCRoute
metadata:
  name: example-grpcroute
spec:
  parentRefs:
  - name: example-gateway
  hostnames:
  - "svc.example.com"
  rules:
  - matches:
    - method:
        service: com.example
        method: Login
    backendRefs:
    - name: foo-svc
      port: 50051
```

## 总结

最后将客户端、这几个资源以及 backend 串起来。（其实是直接搬官网的图和说明）

> Here is a simple example of HTTP traffic being routed to a Service by using a Gateway and an HTTPRoute:
>
> ![A diagram that provides an example of HTTP traffic being routed to a Service by using a Gateway and an HTTPRoute](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/gateway-request-flow.svg)
>
> In this example, the request flow for a Gateway implemented as a reverse proxy is:
>
> 1. The client starts to prepare an HTTP request for the URL `http://www.example.com`
> 2. The client's DNS resolver queries for the destination name and learns a mapping to one or more IP addresses associated with the Gateway.
> 3. The client sends a request to the Gateway IP address; the reverse proxy receives the HTTP request and uses the Host: header to match a configuration that was derived from the Gateway and attached HTTPRoute.
> 4. Optionally, the reverse proxy can perform request header and/or path matching based on match rules of the HTTPRoute.
> 5. Optionally, the reverse proxy can modify the request; for example, to add or remove headers, based on filter rules of the HTTPRoute.
> 6. Lastly, the reverse proxy forwards the request to one or more backends.



