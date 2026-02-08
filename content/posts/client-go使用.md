---
date: 2026-01-10T00:35:06+08:00
title: Client-Go使用
tags: [k8s]
categories: [k8s]
draft: true
---

## scheme



## applyconfigurations

每一个资源接口（比如 `PodInterface`、`ConfigMapInterface`）都有一个 `Apply` 方法，这个方法是用来方便地进行 SSA：

``` go
type ConfigMapInterface interface {
	...
	
  Apply(ctx context.Context, configMap *applyconfigurationscorev1.ConfigMapApplyConfiguration, opts metav1.ApplyOptions) (result *corev1.ConfigMap, err error)
  
  ...
}
```

参数会传一个该资源类型的 applyconfiguration，用来指定你要 apply 哪些字段。applyconfiguration 的字段与类型的字段是一样的，区别是 applyconfiguration 里的字段全部都是指针，如果字段为 `nil`，那么 SSA 将保持资源的该字段值不变。

至于使用方法，举个例子就懂了：

``` go
import (
   ...
   v1ac "k8s.io/client-go/applyconfigurations/autoscaling/v1"
)
hpaApplyConfig := v1ac.HorizontalPodAutoscaler(autoscalerName, ns).
   WithSpec(v1ac.HorizontalPodAutoscalerSpec().
    WithMinReplicas(0)
   )
// SSA，并将apply的字段的fieldManager设置为mycontroller
return hpav1client.Apply(ctx, hpaApplyConfig, metav1.ApplyOptions{FieldManager: "mycontroller", Force: true})
```

如果某个字段本身的 fieldmanager 是 mycontroller，但是如果此次你传入的 applyconfiguration 不包含该字段，虽然这个资源对象的字段值不会改变，但其 fieldmanager 会被删除

## 参考

https://kubernetes.io/docs/reference/using-api/api-concepts/#patch-and-apply
