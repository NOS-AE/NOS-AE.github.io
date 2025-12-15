---
date: 2025-12-08T22:24:11+08:00
title: K8S的Ingress
tags: [k8s]
categories: [k8s]
draft: false
---

## Ingress 是什么

Ingress 是 Kubernetes 中的 L7(应用层，HTTP/HTTPS)反向代理与路由入口。

上一篇介绍了 Service，它与 Ingress 的区别是，Service 负责 L4（传输层，TCP/UDP）集群内部服务发现与负载均衡，Ingress 负责 L7 路由、域名/路径转发，他们一般配合着来使用：

```
用户请求 → Ingress → Service → Pod
```

![ingress-diagram](https://cdn.jsdelivr.net/gh/NOS-AE/assets@main/img/ingress.svg)

Ingress 这个 k8s 资源只是定义了路由规则，真正做反向代理的是 Ingress Controller。虽然 Ingress 是内置的资源，但 Ingress Controller 不是 k8s 内置的 contorller，而是可以根据需求自由选择的。比如官方维护的 ingress-nginx 和 aws-load-balancer-controller，还有第三方的 traefik 等，因此单纯创建 Ingress 并没有任何效果，还需要安装一个 Ingress Controller。

要注意，Ingress 只处理 L7 的协议（只支持 HTTP/HTTPS），创建一个 Ingress 并不会对外暴露任何端口，对外暴露端口是 NodePort 或 LoadBalancer 类型 Service 的工作。

## Ingress 资源

一个最简单 Ingress 作为入门：

``` yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: minimal-ingress
spec:
  ingressClassName: nginx
  rules:
  - http:
      paths:
      - path: /testpath
        pathType: Prefix
        backend:
          service:
            name: test
            port:
              number: 80
```

在说 ingressClassName 字段之前先要介绍下 IngressClass 这个资源类型。一个集群中可以同时运行不同的 Ingress Controller。Ingress 将会选择其中一个 Ingress Controller 来处理，但它并不是直接指定 Controller 的名称或者是什么特殊的 id，而是通过 IngressClass 这个资源。IngressClass 绑定了一个特定的 Ingress Controller。比如下面的 IngressClass 绑定了 nginx ingress controller：

``` yaml
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: nginx
spec:
  controller: k8s.io/ingress-nginx
```

另外，IngressClass 还可以设置 `spec.parameters`。它的用处在于，对大多数 ingress controller（Nginx、Contour）来说，IngressClass 可能需要额外的参数来控制行为，例如：LoadBalancer 具体配置、网关定义、代理行为等，这些参数不写在 Ingress、也不写在 IngressClass 本身，而是写在一个独立的资源中，比如 ConfigMap、甚至是某些 controller 定义的 CRD。IngressClass 的 `.spec.parameters` 就是指向这个外部配置，比如：

``` yaml
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: nginx
spec:
  controller: k8s.io/ingress-nginx
  parameters:
    apiGroup: example.io
    kind: GlobalIngressConfig
    name: global-config
```

最后，Ingress 通过将 `spec.ingressClassName` 设置为 IngressClass 的 `metadata.name` 来指定要使用哪个 IngressClass，从而指定负责处理该 Ingress 的是哪个 Ingress Controller。

### rules 和 backend

rules 用于指定路由规则，将匹配 path 的请求路由到指定的 backend，比如在上面例子中，请求路径匹配 `/testpath` 将被路由到名为 test 的 Service。另外，`spec.defaultBackend` 指定了不匹配任何 rule 的情况下的默认 backend。

除了以 Service 作为 backend 外，还可以使用 resource backend，这种 backend 一般是用将「对静态资源的请求」路由到「对象存储」backend 中。下面展示了 defaultBackend 和 reosurce backend 的示例用法：

``` yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ingress-resource-backend
spec:
  defaultBackend:
    resource:
      apiGroup: k8s.example.com
      kind: StorageBucket
      name: static-assets
  rules:
    - http:
        paths:
          - path: /icons
            pathType: ImplementationSpecific
            backend:
              resource:
                apiGroup: k8s.example.com
                kind: StorageBucket
                name: icon-assets
```

### path 类型

在上面的例子中注意到路由规则中的 path 下面还带一个 pathType，一共有三种 pathType：

- Exact：完全匹配，也就是多一个 `/` 都不行，比如 `/foo` 只能匹配 `/foo`，不能匹配 `/foo/`
- Prefix：基于 `/` 单词分割的前缀匹配，从表面上看就是请求路径最后有没有 `/` 都不会影响匹配结果
- ImplementationSpecific：由 IngressClass（ingress controller）的具体实现决定，可能会实现成 Exact、Prefix 或是别的行为

## Ingress 的类型

### 单 service

``` yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: test-ingress
spec:
  defaultBackend:
    service:
      name: test
      port:
        number: 80
```

Ingress 创建后，Ingress Controller 一般会为 Ingress 分配对外暴露的 IP，此时通过该 IP 访问的所有请求，最终都会转发到名为 test 的 service 的 80 端口。注意，如果你没创建这个 service 的话，此时请求一般会返回 503 Service Unavailable。

### 多 service

就是多个 backend 的意思。

``` yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: simple-fanout-example
spec:
  rules:
  - host: foo.bar.com
    http:
      paths:
      - path: /foo
        pathType: Prefix
        backend:
          service:
            name: service1
            port:
              number: 4200
      - path: /bar
        pathType: Prefix
        backend:
          service:
            name: service2
            port:
              number: 8080
```

### 基于 host 的路由

基于 http 请求的 `host` header 来进行路由。

``` yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: name-virtual-host-ingress
spec:
  rules:
  - host: foo.bar.com
    http:
      paths:
      - pathType: Prefix
        path: "/"
        backend:
          service:
            name: service1
            port:
              number: 80
  - host: bar.foo.com
    http:
      paths:
      - pathType: Prefix
        path: "/"
        backend:
          service:
            name: service2
            port:
              number: 80

```

### TLS

Ingress 通过使用一个包含 TLS 证书+私钥的 Secret 来支持 HTTPS 请求。Ingress 规范是 TLS 在 Ingress Controller 处终止，也就是 `集群外 -> Ingress` 的是 HTTPS 请求，而 `Ingress -> Service -> Pod` 的流量则是明文 HTTP。

如果一个 Ingress 配置了多个证书（多个 secret），那么 Ingress Controller 将会通过 TLS 的 SNI（Server Name Indication）字段来选择证书。SNI 是 TLS 握手协议中传输的一个字段，告诉服务器 "我想访问的是哪个域名"。

``` yaml
apiVersion: v1
kind: Secret
metadata:
  name: testsecret-tls
  namespace: default
data:
  tls.crt: base64 encoded cert
  tls.key: base64 encoded key
type: kubernetes.io/tls
---

apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: tls-example-ingress
spec:
  tls:
  # 注意这里是一个数组，可以配置多个hosts + secretName，即可以配置多个证书
  - hosts:
      - https-example.foo.com
    secretName: testsecret-tls
  rules:
  - host: https-example.foo.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: service1
            port:
              number: 80
```

### 负载均衡

Ingress 本身不能让你配置“高级负载均衡策略”。 你想要更智能的负载均衡（例如会话保持、动态权重）只能在 Service 的 LoadBalancer 层实现，Ingress 只做基本的转发策略。因为 Ingress 是 L7 路由入口，不是高级负载均衡器。Ingress 只做：

- Path/Host 路由
- TLS 终止
- 基础反向代理
- 基础健康检查

它不像 Istio、Nginx（这里是指 Nginx 本身，而不是 Nginx Ingress Controller）、Envoy 那样是高级 L7 网关。高级 LB 特性都在 Service 层，不在 Ingress 层：

```
Client
   |
   v
Ingress Controller (basic LB only)
   |
   v
Service (L4 LB from Kubernetes/cloud LB)
   |
   v
Pods
```

``` 
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: example
  namespace: foo
spec:
  ingressClassName: nginx
  rules:
    - host: www.example.com
      http:
        paths:
          - pathType: Prefix
            backend:
              service:
                name: exampleService
                port:
                  number: 80
            path: /
```

