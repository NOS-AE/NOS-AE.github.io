---
title: "常用Bash命令"
layout: "page"
url: "/bash"
summary: "常用的 Bash 命令和脚本"
ShowToc: true
TocOpen: true
---

## 变量

### 变量展开

``` bash
# 对变量内容进行分割、路径名展开，换行符和空格均视为字段分隔，最终多行的内容会被平铺为一行，以空格分割
echo $var

# 保留内容格式，多行依然输出为多行
echo "$var"
```



## 文件操作

### 查找文件
```bash
# 按名称查找文件
find /path/to/search -name "filename"

# 按类型查找（f = 文件，d = 目录）
find /path/to/search -type f -name "*.txt"

# 查找并删除
find /path/to/search -name "*.log" -delete

# 使用 fd（更快的替代品）
fd "pattern" /path/to/search
```

### 批量重命名
```bash
# 批量添加前缀
for file in *.txt; do mv "$file" "prefix_$file"; done

# 批量替换扩展名
for file in *.txt; do mv "$file" "${file%.txt}.md"; done

# 使用 rename 命令
rename 's/old/new/' *.txt
```

### 文件权限
```bash
# 修改文件权限
chmod 755 script.sh

# 递归修改目录权限
chmod -R 644 /path/to/dir

# 修改所有者
chown user:group file.txt
```

## 文本处理

### 清空文件内容

`:` 是一个内置的 Shell 命令，被称为 **空命令 (No-op)**。它什么也不做，但会返回成功的退出状态 (0)，将其输出（实际上什么也没有）重定向到文件，相当于把文件清空了。

``` bash
:>somefile
```

### heredoc

https://linuxize.com/post/bash-heredoc/

heredoc 用于直接在命令行输入多行文本，并将其输入给命令

``` shell
# 一般形式
[COMMAND] <<[-] 'DELIMITER'
  HERE-DOCUMENT
DELIMITER

# 直接在终端将多行文本输出到文件
cat <<EOF > ~/kustomization.yaml
line 1
line 2
EOF

# 直接在终端将多行文本输出到文件（禁止变量替换，原样输出）
cat <<'EOF' > ~/kustomization.yaml
line 1
$HOME
line 3
EOF

# 使用 <<- 以忽略heredoc的tab缩进，便于在判断或者循环代码块中使用
if true; then
    cat <<- EOF
    Line with a leading tab.
    EOF
fi
```

### grep 搜索
```bash
# 基本搜索
grep "pattern" file.txt

# 递归搜索目录
grep -r "pattern" /path/to/dir

# 忽略大小写
grep -i "pattern" file.txt

# 显示行号
grep -n "pattern" file.txt

# 反向匹配（不包含）
grep -v "pattern" file.txt

# 使用正则表达式
grep -E "pattern1|pattern2" file.txt
```

### sed 文本替换
```bash
# 替换第一个匹配
sed 's/old/new/' file.txt

# 替换所有匹配
sed 's/old/new/g' file.txt

# 直接修改文件
sed -i 's/old/new/g' file.txt

# 删除包含特定模式的行
sed '/pattern/d' file.txt

# 在特定行后插入内容
sed '3a\new line' file.txt
```

### awk 文本处理
```bash
# 打印特定列
awk '{print $1, $3}' file.txt

# 使用分隔符
awk -F',' '{print $1}' file.csv

# 条件过滤
awk '$3 > 100 {print $1}' file.txt

# 计算总和
awk '{sum += $1} END {print sum}' file.txt
```

## 系统管理

### 进程管理
```bash
# 查看进程
ps aux | grep process_name

# 实时监控进程
top
htop

# 杀死进程
kill PID
kill -9 PID  # 强制杀死

# 按名称杀死进程
pkill process_name
killall process_name
```

### 磁盘管理
```bash
# 查看磁盘使用情况
df -h

# 查看目录大小
du -sh /path/to/dir

# 查看当前目录下各子目录大小
du -h --max-depth=1

# 查找大文件
find / -type f -size +100M
```

### 网络操作
```bash
# 查看网络连接
netstat -tuln
ss -tuln

# 测试端口
nc -zv host port
telnet host port

# 下载文件
wget URL
curl -O URL

# 查看本机 IP
ip addr show
ifconfig
```

## Git

> 在 Git 的设计中，有一个非常经典的概念区分：**“porcelain（瓷器）命令”** 和 **“plumbing（管道）命令”**。这其实反映了 Git 的 **两层结构设计**——底层“管道层”（plumbing）与上层“用户界面层”（porcelain）。
>
> **plumbing commands**（底层命令）
>
> - 面向 **脚本、工具和 Git 自身内部机制**。
> - 接口稳定、输出简洁（方便机器读取）。
> - 不负责美观输出，也不提供复杂逻辑。
> - 主要用于直接操作 Git 对象（blob、tree、commit、tag）。
>
> **porcelain commands**（高层命令）
>
> - 面向 **普通用户**。
> - 提供易用的语法和丰富的输出。
> - 通常内部会调用多个 plumbing 命令组合实现功能。
>
> 比如：
>
> ``` bash
> # procelain命令
> git add file.txt
> git commit -m "add file"
> 
> # 等价于下面的plumbing命令
> git hash-object -w file.txt # 1. 计算文件对象哈希并存入对象库
> 
> # 2. 更新暂存区
> git update-index --add file.txt
> 
> # 3. 生成树对象
> tree_hash=$(git write-tree)
> 
> # 4. 生成提交对象
> commit_hash=$(echo "commit message" | git commit-tree $tree_hash -p HEAD)
> 
> # 5. 更新引用
> git update-ref refs/heads/main $commit_hash
> ```
>
> 

### git rev-parse

``` bash
# 获取当前分支HEAD的哈希值
git rev-parse HEAD

# 获取.git的路径
git rev-parse --git-dir

# 获取工作区顶层路径，用于脚本快速找到仓库根目录
git rev-parse --show-toplevel

# 获取commit的所在分支名
# 此处是获取HEAD所在分支名，等同于执行git branch --show-current
git rev-parse --abbrev-ref HEAD
```



## 压缩解压

### tar
```bash
# 压缩
tar -czf archive.tar.gz /path/to/dir

# 解压
tar -xzf archive.tar.gz

# 查看压缩包内容
tar -tzf archive.tar.gz

# 解压到指定目录
tar -xzf archive.tar.gz -C /path/to/extract
```

### zip
```bash
# 压缩文件
zip archive.zip file1 file2

# 压缩目录
zip -r archive.zip /path/to/dir

# 解压
unzip archive.zip

# 解压到指定目录
unzip archive.zip -d /path/to/extract
```

## 实用技巧

### 快捷键
```bash
# Ctrl+C: 终止当前命令
# Ctrl+Z: 暂停当前命令
# Ctrl+D: 退出当前 shell
# Ctrl+L: 清屏
# Ctrl+A: 光标移到行首
# Ctrl+E: 光标移到行尾
# Ctrl+U: 删除光标前的内容
# Ctrl+K: 删除光标后的内容
# Ctrl+R: 搜索历史命令
```

### 进程替换

进程替换允许你把一个命令的输出或输入当作文件来使用，语法如下：

``` bash
<(command) # 把命令的输出当作一个只读文件
>(command) # 把命令的输入当作一个只写文件
```

有趣的例子：

``` bash
# <(ls /etc) 会生成一个临时的伪文件，比如 /dev/fd/63；
# <(ls /bin) 会生成另一个，比如 /dev/fd/62；
# 然后 diff 命令读取这两个“文件”并进行比较。
# 这样 diff 就能比较两个命令的输出，而不需要先写入临时文件。
diff <(ls /etc) <(ls /bin)

# 将目录下所有日志文件的路径，保存到数组变量中
# 通过管道操作符会有问题，比如
# command | mapfile ARR
# 管道的右侧是一个新的shell进程，因此ARR存在于新进程中，命令结束后，ARR在本shell进程找不到
mapfile ARR < <(find -type f -name "*.log")

# 边下载边计算校验值
curl -L https://example.com/file.tar.gz | tee >(sha256sum > checksum.txt) > file.tar.gz

# 主输出同时写入：
# grep error → errors.txt，将error日志写入error.txt
# grep warn → warnings.txt，将warn日志写入warnings.txt
# 以及 all.txt，将所有日志写入all.txt
# 非常适合日志分流。
tee >(grep error > errors.txt) >(grep warn > warnings.txt) > all.txt
```



## Docker 相关

### 容器管理
```bash
# 列出容器
docker ps
docker ps -a  # 包括已停止的

# 启动/停止容器
docker start container_id
docker stop container_id

# 删除容器
docker rm container_id

# 进入容器
docker exec -it container_id /bin/bash

# 查看日志
docker logs container_id
```

### 镜像管理
```bash
# 列出镜像
docker images

# 拉取镜像
docker pull image_name:tag

# 删除镜像
docker rmi image_id

# 构建镜像
docker build -t image_name:tag .

# 清理未使用的镜像
docker image prune
```

## K8S 相关

### 代理

``` bash
# 启动反代localhost:8001，直接能请求api，无需authN & authZ
kubectl proxy

# 启动反代*:8001，并接受所有地址的请求
# 如果不设置--accept-hosts，则只能请求127.0.0.1，访问其它ip会返回Forbidden
kubectl proxy --address='0.0.0.0' --accept-hosts='.*'
```

### 端口转发

用于临时端口转发的调试与访问工具，常用于在不暴露 Service（无需 NodePort / LoadBalancer / Ingress）的情况下，从本地直接访问集群内的 Pod 或 Service。

``` bash
# 转发pod端口，通过http://localhost:8080访问pod的80端口
kubectl port-forward pod/nginx-abc123 8080:80

# 转发service端口
kubectl port-forward svc/my-service 8080:80
```

工作原理：

```
本地端口
  ↓
kubectl 进程
  ↓（HTTP/2 SPDY / WebSocket）
kube-apiserver
  ↓
kubelet
  ↓
Pod 内的目标端口
```

因此，kubectl 本身是代理，连接是通过 apiserver 建立的，不依赖 CNI 网络。

## 性能分析

### 系统监控
```bash
# CPU 使用率
top
htop

# 内存使用
free -h

# IO 监控
iostat
iotop

# 网络流量
iftop
nethogs
```

### 日志分析
```bash
# 实时查看日志
tail -f /var/log/syslog

# 查看最后 N 行
tail -n 100 /var/log/syslog

# 搜索日志中的错误
grep -i error /var/log/syslog

# 统计日志中的出现次数
grep "pattern" /var/log/syslog | wc -l
```
