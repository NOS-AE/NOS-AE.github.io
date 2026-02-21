---
date: 2026-02-02T23:33:41+08:00
title: Golang http源码阅读
tags: [源码阅读,go标准库,http]
categories: [源码阅读]
draft: false
---

> [!note]
>
> 基于go 1.25

## 介绍

golang的标准库中的net/http包提供了http相关的各种工具。本篇作为杂记，记录一些其中我觉得有意思的http工具。在探索源码的过程中，也顺便了解一下http这个我们日常总是在接触的、并且一直在不断演进的应用层网络协议。

每介绍一个工具前，我都会先用一些非常简单的例子，介绍这个工具是用来干什么的。

## ReverseProxy

`httputil.ReverseProxy` 能让你轻松地构建一个反向代理服务器（如负载均衡器或 API 网关）。此时你可能会有一个疑问：我收到了客户端发送来的http包，并且也知道了要转发的后端ip和端口，为什么不直接创建一个client，然后用这个client将包转发到目的后端呢？

确实可以，但其实“转发”这个动作，还得遵循http的规范做一些额外的工作，例如处理转发相关的http头（比如`X-Forwarded-For`）、响应码透传（比如`100 coninue`）等；另外，“转发”不但包含请求的转发，还包括响应的转发，我们要读请求写入上游、读响应写入下游...（注意，在反向代理的语境中，“上下游”是针对响应来说的，响应从“上游”流向“下游”，因此“上游”代表后端，而“下游”则代表向反向代理服务器发送请求的客户端）

总而言之，ReverseProxy就是帮我们干了这些麻烦事，即使我们不是那么了解http协议，也能借助ReverseProxy轻松写出正确的http反向代理程序。

### 基本使用

``` go
package main

import (
	"net/http"
	"net/http/httputil"
	"net/url"
)

func main() {
	// 1. 定义目标后端地址
	target, _ := url.Parse("http://example.com")

	// 2. 创建反向代理实例
	proxy := httputil.NewSingleHostReverseProxy(target)

	// 3. 启动监听
	http.ListenAndServe(":8080", proxy)
}
```

程序启动后，对本地8080端口的http请求都会被转发到example.com。

还有更高级一些的用法，比如作为一个负载均衡器：

``` go
proxy := &httputil.ReverseProxy{
    Director: func(req *http.Request) {
        // 动态选择后端服务器
        targetServer := "http://backend-1.internal"
        target, _ := url.Parse(targetServer)
        
        req.URL.Scheme = target.Scheme
        req.URL.Host = target.Host
        req.URL.Path = target.Path + req.URL.Path
        
        // 记得设置 Host，否则某些后端服务可能会拒绝请求
        req.Host = target.Host
    },
    ModifyResponse: func(res *http.Response) error {
        // 隐藏后端敏感的 Server 头
        res.Header.Set("X-Proxy-By", "My-Go-Gateway")
        return nil
    },
}
```

### 核心源码

我们可以给ReverseProxy设置各种钩子，比如Director, Transport, ErrHandler等，定制一个符合我们业务需求的ReverseProxy。最后，ReverseProxy 最主要就是实现了 `http.Handler` 接口，提供ServeHTTP方法实现透明转发，在处理请求和响应时回调我们的钩子。

`ServeHTTP`分为如下几个步骤：

1. 拷贝原始请求
2. 改写请求，主要是删除hop-by-hop请求头、加上其它请求头等
3. RoundTrip（向后端发送请求）
4. 101协议转换处理
5. 改写响应，主要是删除hop-by-hop响应头、加上其他响应头等
6. 回写响应体

``` go
func (p *ReverseProxy) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	transport := p.Transport
	if transport == nil {
		transport = http.DefaultTransport
	}

	ctx := req.Context()

  // 深拷贝in请求作为out请求（请求体不会被深拷贝）
	outreq := req.Clone(ctx)
  
  // ServeHTTP返回后out请求体应该关闭
  defer outreq.Body.Close()

  // Rewrite是一个更加强大的Director，只能选择其中一个
	if (p.Director != nil) == (p.Rewrite != nil) {
		p.getErrorHandler()(rw, req, errors.New("ReverseProxy must have exactly one of Director or Rewrite set"))
		return
	}

  // 使用Director
	if p.Director != nil {
		p.Director(outreq)
	}
	outreq.Close = false // tcp keep alive out->backend

  // 获取Upgrade头，这个头用来指定客户端要升级为哪个协议，比如websocket
	reqUpType := upgradeType(outreq.Header)

  // 删除那些上一跳发送给下一跳之间的头（这些头经过转发之后就没有意义了），比如Connection、Keep-Alive
	removeHopByHopHeaders(outreq.Header)

  // 客户端指定了传输编码方式为trailers，并且ReverseProxy支持，所以也得加上这个头传给后端（这个是http的一种传输编码，了解即可）
	if httpguts.HeaderValuesContainsToken(req.Header["Te"], "trailers") {
		outreq.Header.Set("Te", "trailers")
	}

	// 同理，因为之前删除了上一跳发给下一跳的头，现在再加回去
	if reqUpType != "" {
		outreq.Header.Set("Connection", "Upgrade")
		outreq.Header.Set("Upgrade", reqUpType)
	}

  // 使用Rewrite
	if p.Rewrite != nil {
		// 是否设置这些头由Rewrite决定
		outreq.Header.Del("Forwarded")
		outreq.Header.Del("X-Forwarded-For")
		outreq.Header.Del("X-Forwarded-Host")
		outreq.Header.Del("X-Forwarded-Proto")

		// Remove unparsable query parameters from the outbound request.
		outreq.URL.RawQuery = cleanQueryParams(outreq.URL.RawQuery)

		pr := &ProxyRequest{
			In:  req,
			Out: outreq,
		}
		p.Rewrite(pr)
		outreq = pr.Out
	} else {
    // 如果没有使用Rewrite，那就是使用了Director
    // Director的方式默认会给out请求加上X-Forwarded-For（而Rewrite更加灵活/可定制化，加不加这个头由调用方控制）
		if clientIP, _, err := net.SplitHostPort(req.RemoteAddr); err == nil {
			prior, ok := outreq.Header["X-Forwarded-For"]
			omit := ok && prior == nil // Issue 38079: nil now means don't populate the header
			if len(prior) > 0 {
				clientIP = strings.Join(prior, ", ") + ", " + clientIP
			}
			if !omit {
				outreq.Header.Set("X-Forwarded-For", clientIP)
			}
		}
	}

  // 避免后面Transport给请求自动加上本来就为空的User-Agent
	if _, ok := outreq.Header["User-Agent"]; !ok {
		// If the outbound request doesn't have a User-Agent header set,
		// don't send the default Go HTTP client User-Agent.
		outreq.Header.Set("User-Agent", "")
	}

  // 在http状态码中，1xx状态码属于中间态状态码（101除外），比如客户端收到100 continue后才会继续发送请求体
  // 但roundtrip不会返回这种中间态状态码，因为RoundTripper接口设计为"一次http请求到最终响应"，这些中间状态码都会被内部消化掉
  // 因此要给roundtrip加一个钩子：当收到1xx响应码的时候，透传给客户端
	trace := &httptrace.ClientTrace{
		Got1xxResponse: func(code int, header textproto.MIMEHeader) error {
			h := rw.Header()
			copyHeader(h, http.Header(header))
			rw.WriteHeader(code)

      // 清理1xx的响应头，不污染终态状态码的响应头
			clear(h)
			return nil
		},
	}
	outreq = outreq.WithContext(httptrace.WithClientTrace(outreq.Context(), trace))

  // roundtrip
	res, err := transport.RoundTrip(outreq)
  
  // 注意此时的错误钩子，传入的是out而不是in请求了
	if err != nil {
		p.getErrorHandler()(rw, outreq, err)
		return
	}

	// 101协议转换，需要单独处理
	if res.StatusCode == http.StatusSwitchingProtocols {
		if !p.modifyResponse(rw, res, outreq) {
			return
		}
		p.handleUpgradeResponse(rw, outreq, res)
		return
	}

  // 删除那些上一跳发送给下一跳之间的头（这些头经过转发之后就没有意义了），比如Connection、Keep-Alive
	removeHopByHopHeaders(res.Header)

  // 响应修改钩子
	if !p.modifyResponse(rw, res, outreq) {
		return
	}

  // 复制响应头
	copyHeader(rw.Header(), res.Header)

	// 将所有Trailer key写入响应头
	announcedTrailers := len(res.Trailer)
	if announcedTrailers > 0 {
		trailerKeys := make([]string, 0, len(res.Trailer))
		for k := range res.Trailer {
			trailerKeys = append(trailerKeys, k)
		}
		rw.Header().Add("Trailer", strings.Join(trailerKeys, ", "))
	}

  // 写状态码和响应头
	rw.WriteHeader(res.StatusCode)

  // 写响应体
	err = p.copyResponse(rw, res.Body, p.flushInterval(res))
	if err != nil {
		defer res.Body.Close()
		if !shouldPanicOnCopyError(req) {
			p.logf("suppressing panic for copyResponse error in test; copy error: %v", err)
			return
		}
		panic(http.ErrAbortHandler)
	}
	res.Body.Close() // close now, instead of defer, to populate res.Trailer

  // Trailer存在的话，要马上输出响应体给客户端，避免自动添加上content-length
	if len(res.Trailer) > 0 {
		http.NewResponseController(rw).Flush()
	}

  // 写Trailer响应头给客户端
	if len(res.Trailer) == announcedTrailers {
		copyHeader(rw.Header(), res.Trailer)
		return
	}

  // 如果响应体关闭后又有了新的trailer，那么重新遍历trailer，一个个写回给客户端
	for k, vv := range res.Trailer {
		k = http.TrailerPrefix + k
		for _, v := range vv {
			rw.Header().Add(k, v)
		}
	}
}
```

golang里的`io.Writer`和`io.Reader`没法直接传输，必须先将数据从reader读到buffer，再将buffer写入writer。因此，对于每个请求，ReverseProxy都会创建一个大小为32*1024的buffer，当请求量大时很容易给gc造成压力，因此ReverseProxy还支持用户设置`BufferPool`，最常见的BufferPool应该就是用golang官方的sync.Pool了。

数据写入writer后，可能会被缓存在writer中，用户可以设置自动flush的间隔，每当数据被写入writer后，就会启动一个定时器，在FlushInterval间隔后调用writer的flush方法将缓存中的数据写入网络中（对于时间间隔不足FlushInterval的两个write之间，会重置定时器，使得定时器从最后一次write调用开始计时）。

HTTP Trailer是一种在消息体发送完成之后再发送的 HTTP 头字段机制，主要用于 分块传输场景。它本质上还是 HTTP header，只是 发送时机在 body 之后。为什么需要trailer？有些 header 的值，只有在 body 完全发送完之后才能计算出来，比如Content-MD5、body 实际大小、流式生成数据时的签名等。

在最初的 header 中用 `Trailer` 头声明：

```
Trailer: Digest, X-Checksum
```

这是为了让接收方提前知道有哪些trailer字段。

最后在body发完之后，就跟着这些trailer头：

```
Digest: xxx
X-Checksum: xxx
```

最后重点看下协议转换这块：

``` go
func (p *ReverseProxy) handleUpgradeResponse(rw http.ResponseWriter, req *http.Request, res *http.Response) {
  // 将后端转换为ReadWriter
	backConn, ok := res.Body.(io.ReadWriteCloser)

  // 劫持客户端的ResponseWriter，拿到一个net.Conn，即ReadWriter
  // 劫持的目的是让其不再受HTTP库管控，而是返回一个TCP连接让调用方去处理
	rc := http.NewResponseController(rw)
	conn, brw, hijackErr := rc.Hijack()

	defer backConn.Close()
	defer conn.Close()

	copyHeader(rw.Header(), res.Header)
	res.Header = rw.Header()
	res.Body = nil // so res.Write only writes the headers; we have res.Body in backConn above
  
  // 将101响应头写回给客户端，以通知客户端后续使用新协议进行通信（注意这里是最后一次用户http协议进行传输）
	res.Write(brw)
  brw.Flush()
  
  // 开始websocket通信
  // 创建两个协程，进行全双工的字节转发
	errc := make(chan error, 1)
	spc := switchProtocolCopier{user: conn, backend: backConn}
	go spc.copyToBackend(errc)
	go spc.copyFromBackend(errc)

	// 等待EOF或错误退出
	err := <-errc
	if err == nil {
		err = <-errc
	}
}
```

在协议转换中，劫持（Hijack）了与客户端之间的TCP连接（conn），并使用这条客户端连接与后端连接（res.Body，即backConn）之间转发原始字节数据。

**劫持**的意思是，本来`http.ResponseWriter`是golang的http库来管控的，给你暴露了一些方法比如`Write`、`WriterHeader`等，让你很方便地给客户端发送http格式的数据，而无需关心http格式的数据应当如何组织（比如加入Content-Length或其他 HTTP 头）。当ResponseWriter被劫持之后，golang的http库把这个连接交给了调用方，并且不会再向它底层连接发送数据，由调用方去调用Write方法发送数据。此后，往里面写的数据都是原始字节数据。



