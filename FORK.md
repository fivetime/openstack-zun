# 这个 fork 改了什么,为什么

上游基点 `e79265e8`。上游 Zun 从 2021 年起是纯维护状态,所以 rebase 便宜,
分歧点也就值得逐条记下来——否则半年后没人分得清哪行是上游的、哪行是我们的。

**唯一分支是 master,直接在上面开发。**曾经散成六条 feature 分支,合回时其中
两条已被主线重做取代,只换来冲突。开分支前先想清楚它要跟谁并行;没有并行就不要开。

---

## 一、这个 fork 服务于什么

KNaaS 的 B2' 算力线:租户的 Kubernetes pod 落成 Zun capsule(Kata 隔离,租户零
worker 节点)。上游的 Zun 是"应用容器服务",心智模型对齐 Nova——容器即虚机,
有终端、能 attach。我们要的是另一半:**capsule 作为 pod 的执行后端**,由
kubezun(virtual-kubelet provider)驱动。

两者不冲突,而且都要保留 —— 见 §4。

## 二、维护边界

| 区域 | 状态 |
|---|---|
| capsule + CRI driver + zun-cni | **主干**,我们的改动几乎都在这里 |
| Container API + DockerDriver | 曾划为"不维护区";§4 起改为**在 CRI 上重建** |
| kuryr_network | 不维护 |

⚠️ **实验环境的 `/opt/stack/zun` 全是手改的工作树**(HEAD = 上游基点 + 未提交
改动),所以"在跑什么"只能算哈希、不能看 git。查证运行时行为要上计算节点
(04/05/06),不要查控制面 node-01——它的 `container_driver` 与计算节点不同,
`cri/driver.py` 在那台上是死代码。

## 三、已经做了什么

按"上游没有 → 我们加了"分组。每条都实测过;没实测的不在这里。

### 3.1 capsule 作为 pod 后端所必需的

- **securityContext 落到运行时**(`cri/driver.py:_linux_security_context`)。
  runAsUser/runAsGroup/fsGroup/readOnlyRootFilesystem/allowPrivilegeEscalation/
  capabilities/seccompProfile。⚠️ 它存在 `healthcheck` 列里,不是 capsule 的
  annotations——API 会用生成名覆盖 capsule 容器的名字,按名字做键永远匹配不上,
  结果是每个容器都以 root 跑且根文件系统可写,**静默地**。
- **capabilities 白名单**(`[container_driver] allowed_capabilities`,默认只
  `NET_BIND_SERVICE`)。API 校验一次,driver 再校验一次:即使被绕过的 spec 进了
  数据库,也到不了运行时。
- **探针**:CRI 路径 fork 出 ExecSync;httpGet/tcpSocket/gRPC 由调用方改写成对
  容器自身的 exec——探针没法从别处够到容器。
- **DNS**:sandbox 的 resolver 由创建者用 annotation 指定
  (`knaas.io/dns-searches` / `knaas.io/dns-nameservers`)。上游 CRI driver 完全
  不配 DNS,capsule 会继承子网的解析器,而那台解析器不认识集群内的名字。
- **单字符容器名/ 卷名**:上游 schema `minLength: 2`,而 Kubernetes 允许一个
  字符。⚠️ 长度和 pattern 都要改——原 pattern `^[a-zA-Z0-9][a-zA-Z0-9_.-]+$`
  自己就要求了第二个字符。

- **capsule 模板收 `securityGroups`**(与 `nets` 并列)。驱动**早就**在读
  `capsule.security_groups` 并传给 port(`cri/driver.py:323-330`),`network/neutron.py:110-113`
  的创建分支也早就会把它写进 port——**只差 API 没地方说这件事**,于是每个 capsule 都
  落到项目的 default 组,这正是"同租户所有 namespace 互通"的直接原因。
  组名在**请求时**就解析成 id(限定在调用者自己的 project 内),不存在的组当场 400
  并点名;放到挂载时才解析,租户看到的会是一个"没说原因就死掉的 pod"。
  ⚠️ **空列表和不写不是一回事**:不写 = 没意见(Neutron 塞默认组),空 = 什么都不放行。
  两者原来都被折叠成 `None`,于是**"要求谁都够不到"变成了"谁都能够到"**——正是执行
  隔离的那条路上,朝着放行的方向错。

### 3.2 存储

- **emptyDir 卷种**(`volume/driver.py:EmptyDir`)。随 capsule 生灭,被挂载它的
  每个容器共享;`medium: Memory` 是 tmpfs,尺寸由内核强制。目录 0777,与 kubelet
  一致——capsule 不知道镜像用哪个 uid,更严的权限会让非 root 工作负载写不了自己
  的暂存目录。
- **NFS 卷种**(`volume/driver.py:NFS`),服务 ReadWriteMany。⚠️ Cinder 的
  multiattach **不是** RWX:它共享块设备而非文件系统,两个写者会静默损坏 ext4。
  **节点自授权模型**:attach 时用请求上下文(租户自己的 token)给本节点 `/32`
  授权,detach 时最后一个挂载走了才回收;永不授权子网。
- **fsGroup,两半缺一不可**:挂载后 chown 到组 + setgid,**并且** fsGroup 要进
  CRI 的 `supplemental_groups`。少任何一半症状完全一样——卷挂上了、属主看着对、
  每次写都 Permission denied,从 pod 里看和坏卷无法区分。
- **文件卷原地改写**(`capsule_update_file`)。为 service account token 续期而
  加:capsule 建成后不可变,而 token 会过期;重建 capsule 会重启工作负载并丢地址,
  exec 进去改需要 shell——**distroless 镜像没有 shell**,实测报
  `the file ls was not found`,恰好在最该照顾的镜像上失效。文件原地截断重写
  (不能 rename,那会断开 bind mount)。

### 3.3 可观测与运维

- **capsule stats**(`capsule_stats` + `/v1/capsules/{id}/stats`),CRI
  `ListContainerStats`,按容器给。CPU 是**累计纳秒计数器**,原样上报不转速率——
  只有调用方知道哪条早先读数属于同一个容器,而容器重启会把计数器清零。
- **流式 exec**(`_create_streaming_exec` + `zun-wsproxy` 上计算节点)。见 §4.2。
- **Placement 分配泄漏**:上游只在 `ResourcesUnavailable` 时归还分配,其他任何
  建容器失败都留下一条,无人回收。两节点实测攒到 414 条,节点报满而几乎没跑东西。
  修了创建路径 + 周期清扫。⚠️ 清扫在 `host_shared_with_nova` 时**不运行**:
  nova 的分配在同一个 RP 上,分不清哪条是自己泄漏的,而删掉一个在跑的实例的分配
  比留下泄漏坏得多。
- **孤儿节点资源清扫**:卷走了但挂载/rbd 映射留下。不是美观问题——映射着的 rbd
  镜像持有 Ceph watcher,Ceph 拒绝删除被 watch 的镜像,表现为"卷可用但删不掉,
  且没有任何东西看起来占着它"。两种形状都实际发生过。

- **shim 已死的 capsule 现在删得掉**。停止和删除原来写在同一个 try 里,而 shim 一没,
  `StopPodSandbox` 就去等一个没人应答的任务、`DEADLINE_EXCEEDED`,**把删除一起带走**。
  于是永远删不掉:每次重试都在等同一个不存在的 shim,记录还在、资源账还在,
  **而没有任何东西看起来占着它**——和 rbd watcher 同一形状。
  **删除才是目的,停止只是礼貌**,两者现在分开尝试(CRI 本来就规定
  `RemovePodSandbox` 要强制终止里面还在跑的东西)。实测:那批 8-06 起删不掉的,
  `DELETE` 从 500 变 204,租户 capsule 从 29 个降到 5 个(全部有 pod 在跑)。

### 3.4 镜像

- **`commit` 能推到 registry**(2026-08-28)。上游把上传目标**硬编码成 glance**
  (源码注释:*"Glance is the only driver that support image uploading"*)。
  镜像面是 registry 的部署上,这产出的是**存在但看不见的镜像** —— 不出现在镜像列表、
  不能按名字跑、不经任何镜像门禁。现在**仓库名指明了 registry** 就在节点上 commit 完
  再 push(判据用 docker 自己的规则:首段含点或冒号),其余仍走 glance,上游行为不变。
  - push 必须**流式读到底**:客户端把失败写在流里而不是抛出来,不读的话
    **被 registry 拒绝的上传会以成功收场**,调用方被告知有个并不存在的镜像在等它。
  - 凭据按**目标 host** 选,不按容器自己的 registry(那是镜像的**来源**)。
    拿一个 host 的凭据去推另一个,被拒为 `malformed HTTP Authorization header` ——
    既不点名 host 也不点名凭据,读起来像客户端坏了。没有匹配凭据就匿名推,
    失败是 `unauthorized`,说的是真话。
  - commit 同步、push 异步:commit 快且产出调用方要的 id,push 慢且与 id 无关。
    所以 202 的含义是"镜像做出来了",不是"已经到位了"。

## 四、双驱动:capsule 给 K8s,container 给 Horizon

**2026-08-11 定案。**

### 4.1 为什么

上游 Zun 的"容器即虚机 + 终端"服务的是**不需要理解 K8s 和集群概念的用户**:
在 Horizon 里点几下建一个容器,开个终端就能用。这条产品线有价值,不该因为我们
把重心放在 capsule 上就消失。

zun-ui 只用 Container API,终端只有 attach(`zun_ui/api/client.py:259`),而且
要求容器**创建时**就 `interactive=true`——终端是出生时决定的,这正是"容器即虚机"
的语义。

### 4.2 为什么不能直接用现成的 DockerDriver

分派机制本来就是双的(`manager.__init__` 两个槽位,`_get_driver` 按对象类型分派),
配 `container_driver = docker` + `capsule_driver = cri` **零开发**就能让 zun-ui
的终端可用。但它给不了我们要的东西:

dockerd 管它自己那套 containerd(`moby` 命名空间),而 capsule 在 `k8s.io`。
装上 dockerd 之后是两套镜像存储、两套 kata sandbox、VMM 各起各的、资源账分裂,
并且在 containerd 之上再绕一层 dockerd——而 docker 自己用的就是 containerd。

**定案:废弃 DockerDriver,让 CriDriver 实现 ContainerDriver。**后端只有一套
containerd + kata + VMM,一份资源账。

**"废弃"不等于"破坏"**:`container/docker/driver.py` 整个 fork 期间零改动,
分派机制也没动,配 `container_driver = docker` 照样能跑。⚠️ 但共享代码上踩过一次:
为 CRI 改 wsproxy 时把 TLS 证书从 `CONF.docker.*` 挪成构造参数、又无条件下发了
运行时的子协议,两处都只有 docker 那条路会疼(远程 daemon 连不上、子协议不被认)。
现在由 `_target_options()` **按 URL 判别**——运行时给 `http://`,docker daemon 给
`ws://`,各拿各需要的。**改共享代码时要问的不是"我这条路对不对",而是"另一条路
还在不在"**。

### 4.3 为什么这比想象的近

**在 CRI 上,一个"容器"就是只有一个容器的 capsule。**CriDriver 已经在做建
sandbox、建容器、起停、挂卷、取日志、exec、stats——那是 capsule 路径每天在跑的。
ContainerDriver 缺的 30 个方法大多是**薄适配**,不是新实现。

### 4.3.1 CRI 能做什么,做不到什么

**按能力划线,不按"薄不薄"猜。**⚠️ 曾把 pause/unpause 列进"第二档,都薄",
错在两头:它们不薄(CRI 没有这个 RPC),但也不是做不到(**containerd 有**)。

| ContainerDriver 方法 | 状态 |
|---|---|
| create/delete/start/stop/show/list/`get_websocket_url`/镜像 | 已实现并实测 |
| reboot / update / stats / top | 已实现并实测(CRI) |
| **pause / unpause / kill(带信号)** | 已实现并实测(**containerd task API**,见 §4.3.2) |
| **resize**(tty 尺寸) | **做不到**,见下 |
| **network_attach / network_detach** | **做不到**,见下 |
| commit / get_archive / put_archive | **CriDriver 没有这些方法**,落到基类的 `NotImplementedError`。⚠️ `commit` **现在有需求了** —— DaaS 网关 2026-08-28 开通了 `docker commit`,走 DockerDriver 的实现;CRI 路径要用得先补 `commit`/`push_image` |

#### 真正做不到的两项(以及为什么不是"还没做")

- **`resize`**。终端尺寸**已经**能改——它走流内第五通道,`kubectl exec -it` 里
  实测好使。够不到的是**从流外面改**:REST 的 resize 是另一条连接,而 proxy 没有
  会话表把它和某个开着的流对上;运行时也没有这个调用。要做就是给 wsproxy 加会话
  跟踪,是架构改动不是补方法。**现在明确报错并说明原因**,不返回 500。
- **`network_attach` / `network_detach`**。沙箱的网络在创建时定死。kata 内部确实有
  `Sandbox.AddInterface`(`virtcontainers/sandbox.go:1245`),但 **shim 的管理接口
  只暴露 `/direct-volume/resize` 一类,不暴露它**;CRI 也不会对运行中的 sandbox
  重跑 CNI。要做就得绕开 CRI 直接操 os-vif + kata 热插,那是另一个架构决定。

#### 4.3.1a CriDriver 补齐的三类方法(2026-08-30,测试床实测)

`commit`/`push_image`、`get_archive`/`put_archive`、`add/remove_security_group`
原本只有 DockerDriver 有,CRI 节点上落基类的 `NotImplementedError`。现已补齐:

- **安全组**:容器的安全组在它的 neutron 端口上,运行时从来看不见。docker 驱动经 kuryr 去改,
  这个驱动的端口是自己建的,就自己改。实测:加/删在端口上真实生效,原有组不动。
- **`docker cp`**:kata 容器的文件系统在虚拟机里,宿主机上没有路径可写,两个方向都得在容器里跑 tar。
  读走同步 exec;**写不能走流式** —— containerd 只提供 `v4.channel.k8s.io`,
  **v4 没有任何帧能表示"stdin 结束"**,`tar -xf -` 于是永远等不到 EOF、调用挂到超时。
  改为分块经同步 exec 送入,每块自带退出码。⚠️ **块大小有硬上限**:实测命令总长
  64 KiB 可用、128 KiB 返回 exit 7 且 stderr 为空(说不出原因的拒绝),现取 32 KiB 原始数据。
  实测:1 MiB 二进制往返 md5 完全一致。
- **`commit`/`push`**:CRI 没有这两件事,越过它去用 containerd 的 diff/content/images
  三个服务。**层交给 diff 服务算**——容器里的删除在层里是 whiteout,拿 overlay 的 upperdir
  自己拼,committed 镜像会把租户删掉的文件带回来。推送没有 docker 守护进程可托付,直接走
  registry HTTP(已存在的 blob 不重传,基础镜像在同一 registry 其他仓库里的用 cross-repo mount)。
  实测:commit 3.8s 完成;**推送闭环已用 Harbor robot 账号验证** ——
  `cri-commit-test/proof:v6` 落到 Harbor(OCI manifest / 3.4 MB / 标签正确)。

  ⚠️ **自写 registry 客户端的代价:认证一处踩了四个坑,症状全是同一个 403。**
  1. 挑战按逗号切 —— `scope="repository:x/y:pull,push"` 的值里也有逗号,被切成
     `scope=...:pull` 外加一个凭空的 `push"` 字段。要按 `key="value"` 解析。
  2. **token 范围要按用途要,不能跟着挑战走** —— push 的第一个请求是 HEAD,
     挑战只说 `pull`;后面上传需要 `push`,而 registry 对"范围不足"回 **403 不是 401**,
     只认 401 的重试永远不会去换更好的 token。
  3. **Harbor 的 cookie 会压过 Bearer 头** —— 带 cookie 403、去掉 202(同一个 token)。
  4. 而且**清一次不够**:清完之后紧接着那个响应又把 cookie 种回来了。要全程拒收。

  ➡️ **已改用 containerd 的 transfer 服务,`registry.py` 整个删掉。**
  源 `ImageStore` → 目标 `OCIRegistry`,认证经 `RegistryResolver.headers` 给 Basic 头
  (另一条路是 auth_stream,那要再实现一个 streaming 服务,而 Basic 已经够说清楚)。
  实测:`cri-transfer-test/app:v2` 推到 Harbor,OCI manifest / 3.4 MB / 标签正确。
  ⚠️ **这里有第三个"包名即契约"的坑,而且更隐蔽:**
  `Any.Pack()` 写的是 `type.googleapis.com/<全名>`,而 **containerd 的 typeurl 按裸 proto
  全名注册和查找**(`FullName()`)。带前缀 → `ResolveType` 查不到 → 消息按自身反序列化而不是
  按它代表的东西 → 报 **"method Transfer not implemented for A to B"**,读起来像功能没实现。
  必须手工构造 `Any(type_url=msg.DESCRIPTOR.full_name, value=msg.SerializeToString())`。
  **判据:用 `ctr images push` 推同一个镜像**(走同一个服务)—— 1.4 秒成功,
  服务端与请求端的责任立刻分清。

🔴 **两条踩得最狠的坑,都不是逻辑错:**
1. **最小 proto 可以少字段少调用,但不能改包名。** gRPC 按 `包.服务` 路由,
   我给三个新服务用了自家包名,containerd 回 `unknown service` —— 它明明实现得好好的。
   `tasks.proto`/`snapshots.proto` 一直是对的,新加的三个一开始不是。
2. **server-streaming 在 eventlet 下必挂。** gRPC 内核在原生线程上发完成信号,
   而要唤醒的等待者是只有 hub 能跑的绿线程。实测:同一个 commit 在普通解释器里 0.2s,
   unary 调用在 eventlet 下正常,**流式读挂到连 `eventlet.Timeout` 都打不断**,
   `tpool` 也救不了(池里的原生线程拿的仍是被 patch 的原语)。
   症状是 **compute 心跳停了、调度器把整台机器判成 down**,而报错只说"主机没上线"。
   现在 commit/push 整体在子进程里跑(参数走 stdin,免得 registry 密码出现在 `ps` 里)。

#### 4.3.1b 方法缺口的最终账目(2026-08-30)

补齐后,**DockerDriver 有而 CriDriver 没有的只剩 4 个,且基类默认值都是对的**:
`get_available_nodes`(=本机)、`get_total_disk_for_container`(psutil 读 `/`)、
`get_host_default_base_size`(None)、`node_support_disk_quota`(False,见下)。

**本轮补的:**
- 🔴 **`sample_counters`** —— 之前落基类返回 `{}`,**计量链路对 CRI 容器一个数都拿不到**,
  而且不报错。这是最危险的一类:计费照跑、报告为空、没有任何东西说话。
  现用 `ListContainerStats` + `ListPodSandboxStats` **两次调用覆盖整台机**
  (网络只在 sandbox 级有,容器级消息根本没有网络字段)。
  实测:CPU 92.7 ms / 内存 962 KB / eth0 rx 746 · tx 2564。
  ⚠️ 两个字段**故意留空而不是编**:`system_ns`(CRI 只报容器烧了多少,没有全机口径)、
  `pids`(CRI 的容器统计里没有)。空表示"没有",除以一个编出来的数比没有更糟。
- `delete_image` —— 交给运行时;还被容器占用的镜像由运行时拒绝,不在这里另立规矩。
- **五个没有对应物的路径改为诚实拒绝**(原先落基类 `NotImplementedError` → 租户看到 500):
  `create_image`/`upload_image_data`/`delete_committed_image`(commit 到 glance 那条路,
  指路"提交到带 registry 的名字")、`create_network`/`delete_network`
  (CNI 在 sandbox 启动时给接口,这里没有可预先创建的东西,指路 neutron)。

**🔴 `node_support_disk_quota` 保持 False,这是结构性的不是没做:**
containerd 的 **overlayfs 快照器完全没有配额能力**(只有 `upperdir` 标签;只有 devmapper
的 thin device 有大小)。而且测试床 `/var/lib/containerd` 在 ext4 上、无 `prjquota` 挂载选项。
zun 现有行为已经诚实:传了 `disk` 就明确拒绝、没传就忽略默认值。
⚠️ **对 DaaS 无影响** —— DaaS 的 `_LIMITS` 只映射 `PidsLimit`/`BlkioWeight`,从不发 `disk`。

**结论:就 DaaS 网关这条路而言,CriDriver 已无已知功能缺口**(run/exec/logs/cp/commit+push/
安全组/计量/可写层计费/stop-start 保层全部实测通过);剩余差异都在 zun 原生 API 上,
且都已从 500 改成能读懂的拒绝。

#### 4.3.1c CriDriver + gVisor 实测(2026-08-30,测试床三台)

**结论:能跑,全功能通过。** 容器内核 `4.19.0-gvisor`;run / exec / `docker cp` 双向
(1 MiB md5 一致)/ **stop-start 保住可写层** / commit(快照 diff 出正确 manifest)/
计量(CPU 六秒 +6.49e9 ns ≈ 1.08 核、内存、网络 rx/tx、**可写层 1064960 字节**)全部实测通过。

**两个必须的配置,少一个就出错:**
1. 🔴 **`overlay2 = "none"`** —— 默认 `root:self` 把容器根文件系统的写入留在沙箱内,
   `stop/start` 丢数据、按可写层计费读到 0。设了之后可写层字节能正确读出。
2. **不要设 `systemd-cgroup`** —— 这套 containerd 的 runc/kata 都没启用它,
   zun 传下来的是 `/zun.slice/<uuid>/<id>` 这种文件系统路径,而 runsc 开了 systemd-cgroup
   会拒:`invalid systemd path`。跟着 runc/kata 走 cgroupfs 即可。

**注册方式**:照 kata 的样子放 `conf.d/55-runsc.toml`(**新增处理器,不动默认运行时**,
那几台还跑着 k8s 工作负载);zun 侧 `container_runtime = runsc`。
⚠️ `crictl info` 的 handlers 一直是空(kata 也一样),**别拿它判断注册成功**,
用 `containerd config dump | grep runtimes.runsc`。

**冷启动:在 zun 这条路上,两者差别被淹没了。**
gVisor 16.2–16.6 s vs kata 18.3–19.4 s(同一条 zun 路径、各 3 次)。
运行时本身的差距是 0.5 s vs 4.8 s(裸 docker 实测,见 D12),
但 zun 一次创建里还有调度、neutron 建端口、CNI、镜像检查、数据库写入 ——
**运行时只占其中一小段,换运行时省下的 4 秒被 16 秒的编排吃掉了大半。**
要提冷启动,该优化的是编排而不是运行时。

#### 几处语义不等价(不是实现细节)

- **pause 不释放任何东西**。链路:shim `Pause` → `Sandbox.PauseContainer` → agent
  `pause_container`(`src/agent/src/rpc.rs:972`)→ `LinuxContainer::pause`
  (`rustjail/src/container.rs:306`)→ **freezer cgroup**。⚠️ 冻结发生在
  **guest 内部**,宿主机毫不知情:VMM 照旧持有整块 guest RAM,Placement 的 claim
  一动不动。省下的只有 CPU 时间。**产品文案不能让它读起来像"暂停就不计费"。**
- ~~**stop/start 与 reboot 会丢掉可写层**~~ **已修(2026-08-30,marker 实测保住)**。
  CRI 仍不能重启已退出的容器,替身照旧在原沙箱里重建 —— 但在 create 与 start 之间,
  新旧两个可写层同时以同一条 overlay 链的 upperdir 形式存在于宿主机上,把旧的
  `cp -a` 进新的(连 whiteout 一起),语义即与 docker 的 stop/start 等价。kata 不改变
  这一点:guest 经 virtiofs 写的就是宿主机这个目录。可写层的位置问 snapshot 服务
  (`snapshots.proto`,与 tasks.proto 同一种最小声明、同一个 socket、同一个
  `containerd-namespace: k8s.io` 头)。**尽力而为**:移植失败退回
  原来的"从镜像重建",`REBUILT_REASON` 只在真丢了的时候出现。

  ⚠️ **2026-08-31 两处更正,都是换 erofs 时炸出来的**(见 4.3.1d):
  ① **可写层不一定是目录**。erofs 被要求限大小时,可写层是一个**定长文件系统镜像**,
     overlay 的 `upperdir={{ mount 0 }}/upper` 是**模板**,只有挂载活着时才被解析 ——
     而替换容器时两个快照都没挂载,于是 `cp -a` 跑成
     `cp -a {{ mount 0 }}/upper/. {{ mount 0 }}/upper` 失败,写的东西全丢。
     现在按形状分支:目录照旧 `cp -a`,镜像则**整份拷贝镜像文件**
     (containerd 的 mkfs 挂载处理器对已存在的镜像跳过格式化,所以拷过去就是全部);
     两边形状不同、或两个镜像大小不同,则**拒绝迁移**退回重建。
  ② **snapshotter 名不再读 `cri_snapshotter`**。那是 containerd 配置的第二份拷贝,
     一改一不改就静默漂移(换 erofs 时正好触发)。该选项默认改空 = **问运行时**
     (CRI `Status(verbose)` 的 `config.containerd.runtimes.<handler>.snapshotter`),
     显式设置仍然优先。
- **`stats` 的 CPU 必须采两次**。运行时给的是累计纳秒计数器。BLOCK/NET I/O 运行时
  不记,报 `-/-` 而不是 0——0 读起来是"空闲",不是"没人量过"。

#### 4.3.1d 三个"接受了然后丢掉"的字段,和 per-container 磁盘配额(2026-08-31)

**判据统一是:要么兑现,要么明说做不到 —— 不许静默丢。** 本轮四件事同源。

**① rootfs 配额(erofs)。** 原判"CRI 上 per-container 配额结构性做不到"**只对发布版成立**:
overlayfs 无任何容量机制、devmapper 只有全节点统一的 `base_image_size`,而
**erofs 给每个活动快照建定长 ext4 镜像做可写层**、读
`containerd.io/snapshot/max-size` 标签 —— 但该标签与 `default_size` 都**只在 containerd main**,
生产是 v2.3.4,所以在实机上怎么找都找不到。

- `node_support_disk_quota()` 不靠 operator 声明,而是读 **CRI `Status(verbose)`** 里
  `config.containerd.runtimes.<handler>.snapshotter`,只有 `erofs` 算支持;
  **未知一律算不支持**(宁可明确拒绝)。调度器链路自动通(基类 `get_available_resources`
  本来就报 `disk_quota_supported`)。
- `container.disk`(GiB)→ 容器注解 `containerd.io/snapshot/max-size`(字节)。
- ⚠️ **containerd 的 CRI 层原先不把注解继承到容器快照**(沙箱快照继承,容器快照不继承),
  已提上游 **[containerd#14070](https://github.com/containerd/containerd/pull/14070)**。
  上游未做的原因:`rootfs_size_in_bytes` **只存在于 `WindowsContainerResources`**,
  Linux 侧 CRI API 没有对应字段,只能走注解。
- 实测(测试床三台,kata guest):`--disk 3` 的容器在 2.9 GB 处 `No space left on device`,
  不指定的取 `default_disk` 在 9.7 GB 处停;宿主根盘不受影响。

**② `hostname`。** DockerDriver 早已实现,**CriDriver 的 `PodSandboxConfig.hostname` 一直空着**
(CRI proto 里有这个字段)。实测:容器要 `probe-host`,里面叫 `9f658c037160`。已设。

**③ `entrypoint`。** 同上,`driver.py` 里原本只有一行 `TODO(hongbin)`。
CRI 的 `ContainerConfig.command` 就是入口、`args` 是参数 —— 与 docker 同一个划分、
不同的名字;**`command` 留空才是"用镜像自带的入口"**,所以只在被要求覆盖时才设。
实测:`--entrypoint /bin/echo` 原本零输出且退出码 0。

**④ `user` —— 这个字段 zun 容器 API 从来没有过**(只有 capsule 的
`securityContext.runAsUser`,存在 healthcheck 列)。补成正式字段,**微版本 1.50**:

- 按 docker 原样保存(`uid` / `uid:gid` / `name` / `name:group`)——
  名字只有镜像能解析,在这里拆开就是替镜像做决定;
- **DockerDriver** 透传;**CriDriver** 拆到 CRI 的三个字段
  (`run_as_user` / `run_as_group` / `run_as_username`);
- ⚠️ **CRI 没有 `run_as_groupname`** → **用名字给的组明确拒绝**。
  静默丢掉会让容器进了一个它没要的组,写出的文件本该同组的人反而读不到。
- 留空时镜像自带的 `USER` 仍然说了算;传空字符串会把它覆盖成 root,那不是"未设置"的含义。
- 🔴 **一个新字段要改三处**:`zun` → **`python-zunclient` 的 `CREATION_ATTRIBUTES` 白名单**
  → 调用方。漏了中间那处,请求**在客户端就被挡下、根本到不了 API**,
  症状是 `InvalidAttribute: Key must be in name,image,...`,读起来完全像后端不支持。

**⚠️ 上线顺序(踩过):加列的迁移必须先于新代码。** 反过来做,新代码启动即查不存在的列,
`zun-compute` 崩在 `SystemExit: 1` —— 而 **pod 显示 Running、重启 0 次、
`rollout status` 报成功**,只有"容器永远停在 Creating、没有 host"这一个症状。
`rollout status` 成功不等于服务在工作。

#### 4.3.1e 三种"限制"的最终账目(2026-08-31/09-01)

CRI 的 `LinuxContainerResources` **只有 cpu / memory / swap**,再无别的。
所以 pids、blkio 都不能经 CRI 表达 —— 但**三者的结论不一样**,不能一概而论。

| 限制 | 结论 | 为什么 |
|---|---|---|
| **swap** | ✅ **已实现** | CRI 有 `memory_swap_limit_in_bytes`,本驱动原先无条件写成 `= memory`(等于关 swap)、不读 `container.swap`。已改为发**总量**(memory+swap,与 docker 同一口径),`-1` 透传 |
| **pids_limit** | ❌ **明确拒绝,且这是终态** | CRI 无字段;containerd 从不从 CRI 设 OCI `Resources.Pids`;唯一逃生口 `unified` **kata-agent 的 cgroup 管理器根本不读**(两套管理器都不读)。⚠️ **更重要的是不需要**:VM 运行时下 guest 的 PID 空间是它自己的,fork bomb 耗尽的是租户自己的 guest(内存固定、vCPU 有 quota),**邻居不受影响** —— 这是自伤限制,不是保护边界 |
| **blkio_weight / device_*_bps/iops** | ✅ **已实现,写在宿主机** | 容器的读写**经 VMM 落到节点的盘**,那才是共享资源;限在 guest 里只是限住它对自己虚拟盘的看法。写在沙箱自己的宿主 cgroup 上(VMM 就在里面) |

**宿主侧 IO 限制的三个实测细节**(都不能靠猜):

1. 🔴 **containerd 建父 cgroup 时只启用它自己要用的控制器**(`cpuset cpu`),
   所以沙箱 cgroup 里**只有 `io.pressure`,没有 `io.max`/`io.weight`**。
   要先沿父链把 `io` 写进 `cgroup.subtree_control` —— **对已经跑着 VMM 的叶子也生效**,
   实测写完文件立刻出现且可写。
2. 🔴 **设备必须是整盘,不能是分区**。`253:2`(vda2)写进 `io.max` **不报错但匹配不到任何 bio**;
   `253:0`(vda)才读得回来。DockerDriver 的 docstring 里早就写着这条,现在两个驱动一致。
3. `blkio_weight` 是 docker 的 10..1000,`io.weight` 是 1..10000,**用 runc 的换算公式**
   (`1 + (w-10)*9999/990`),两个驱动上同一个数字含义相同。

**cgroup 路径公式**(用新建沙箱验的):`<cgroup_parent>/kata_<sandbox-id>`——
`kata_` 前缀来自 kata 自己(`resCtrl.RenameCgroupPath`,它管的控制器就用这个名字标出来);
runc 形态则是 `<cgroup_parent>/<sandbox-id>`,两种都找。

**拒绝点在 compute manager**,紧挨 disk 配额那条检查:节点做不到就在**调用方还在听的时候**
拒绝;过了那里 create 是 cast,拒绝只进日志。驱动经基类新增的
`unenforceable_limits()` 声明自己做不到什么,**默认是"什么都能应用"**,DockerDriver 不受影响。
到了下发这一步再失败就**直接抛错**——拒绝已经做过了,此时失败就等于"接受了然后丢掉"。

**实测**(三台一致):`blkio_weight=500` → `io.weight default 4950`;
`device_read_bps=10485760 device_write_bps=5242880` →
`io.max = 253:0 rbps=10485760 wbps=5242880 riops=max wiops=max`;
`blkio_weight=1000` → `io.weight default 10000`。

⚠️ **粒度是沙箱级**:限的是整个 VM 的 IO。对 zun"一个 capsule 一个 VM"正好,
多容器 capsule 上这几个值取自沙箱的那个主体。

#### 4.3.1f restart_policy:CRI 没有,zun 自己管(2026-09-01)

**原状**:`--restart always` 到了记录里就没人读 —— 只有 DockerDriver 实现(交给 dockerd),
CriDriver 一个字都不读。CRI 节点上容器死了就死了,租户没有任何提示,而 DaaS 是发这个字段的。
CRI 本身没有重启策略:k8s 是 **kubelet** 在外面盯着状态重建,所以这里也得 zun 自己盯。

**判据放在哪 —— 第一版做错了。** 我先写成"状态同步时,上一轮记录是 RUNNING、这一轮运行时报 EXITED,
即为自己死的"。实测**一次都没触发**:`container_show` 走 `driver.show()` 后会
`container.save()`,**API 的 show 先于 sweep 看到退出并把 STOPPED 写进库**,sweep 加载记录时
"上一轮"已经是 STOPPED。DaaS 持续 inspect,这条路在生产上永远先到。
⇒ **不能依赖"是谁先观察到";主人的意图要在 stop 发生的地方记下来。**

**定稿**:
- `stop()` 把 `status_detail` 记成 `STOPPED_BY_OWNER`(`'stopped'`);`_record_exit` 不覆盖
  已有标记(否则 SIGKILL 的 137 会把主人的 stop 记成一次崩溃);容器再次 RUNNING 时清掉。
- sweep 里:`status == STOPPED` 且 `status_detail` 以 `exit:` 开头 = **死的**,按策略处理;
  带 `stopped` 标记的永不复活。与观察顺序无关,幂等。
- 决定在**与 stop 路径同一把锁**下、对记录**重新读一次**后做:库里若已是主人标记、
  `task_state` 在飞、DELETING/DELETED、或主人自己已经 start 成 RUNNING —— 主人先到,不动。
- `always` 与 `unless-stopped` 在这里是同一个策略(二者只在 daemon 重启时有差别,本驱动无此事)。
  `on-failure` 只重启非零退出,`MaximumRetryCount` 用完即止(0=不封顶),计数复用探针重启那份
  `k8s_probe_state.restarts`;**退出码读不到的不重启**(对一个问不到的运行时循环重启更糟)。
  用完重试**在记录上说一次**(`status_reason`),不每轮刷日志。
- **重启动作分两条路**:capsule 成员走探针用的 `_restart_container`;**普通容器走 start 路径的
  `_restart_exited`** —— 🔴 第一版用了前者,炸出 `failed to find sandbox id`:普通容器的
  `container_id` 是**容器** id 不是沙箱 id,得按 OWNER_LABEL 找沙箱。`_restart_exited` 还会
  **把可写层带过去**,这与 docker 的重启语义一致(docker 重启也保留可写层)。
- 节奏 = sync 周期(60s)一次,这就是全部的退避。

**实测**(node-04,期间持续 show 模拟 DaaS):`always` + `exit 1` → 5 个周期重启 5 次、
attempt 到 5、每次"with its writable layer carried over";`on-failure:1` + `exit 3` → 重启 1 次
后停,记录 `used all 1 of its retries`;`on-failure` + `exit 0` → 不重启;`always` + 用户 stop →
`status_detail=stopped`,不复活。

#### 4.3.1g cpu_policy=dedicated 明确拒绝;securityContext 两驱动对齐(2026-09-01)

**`cpu_policy=dedicated`:不是 CriDriver 缺口,两个驱动上都从没工作过,坏在驱动之前。**
实测 `cpu=2 dedicated` → claim 阶段 `KeyError: 'cpuset'`,容器卡 Creating、无任何提示。四处独立缺陷:

1. 只有 **`CpusetFilter`** 会往 `limits` 里放 `cpuset`,而它**不在默认 `enabled_filters`**。
2. NUMA 拓扑**按 `lscpu` 的 socket 分组**(`os_capability_linux.get_cpu_numa_info`),KVM 客户机
   "一 socket 一 vCPU"→ node-04 采到 16 个 socket、1 个内存节点,zip 后**只剩一个节点、cpuset={0}**。
3. 两处类型错(已复现):`claims.py:118 set(numa_node.id)` 对 int;`tracker.py:482 int(cpuset_mems)` 对 set。
4. DockerDriver 把 Python `set` 直接交给 docker-py —— 生产 pod 实测 `TypeError: Object of type set
   is not JSON serializable`。

另有产品语义:启用 `CpusetFilter` 后,`enable_cpu_pinning=True` 的主机**拒绝所有 `shared` 容器**
(上游把绑核主机当独占池),与"租户视角一台弹性机器"相悖,DaaS 也从不发 cpu_policy。
**决定:在 API 层明确拒绝 `dedicated`**(`containers.py` create 抛 `InvalidValue`,调用方还在听),
update schema 本就不含 cpu_policy。要真做,四处都得修,外加独占池语义要不要 —— 另立项。

**`securityContext`:唯一一处方向反过来的差异,已对齐。** capsule 的 `securityContext`
(`runAsUser`/`runAsGroup`/`fsGroup`/`readOnlyRootFilesystem`/`allowPrivilegeEscalation`/
`capabilities`/`seccompProfile`)原本**只有 CriDriver 实现,DockerDriver 一字不读** ——
同一个 pod spec 落到 docker 节点上以 root、可写根、全能力运行,静默。丢的是**收紧**,最危险的一类。
读法抽到基类 `driver.security_context_of()`,DockerDriver 的 `_apply_security_context()`
逐字段照 `_linux_security_context()` 翻成 docker 参数(`user`/`group_add`/`read_only`/
`security_opt`/`cap_add`+`cap_drop`,`allowed_capabilities` 白名单同样在驱动层再挡一次)。
docker-py 7.1 五个参数名在生产 pod 里验过。⚠️ 生产镜像未重打,这条要随下次 zun 发版上线。

### 4.3.2 越过 CRI 那一层:什么时候可以,怎么做

**定案:只对"CRI 之外别无他处"的调用越界。**CRI 服务得了的,一律走 CRI。

containerd 的 task 服务在**同一个 socket** 上,kata 的 shim v2 实实在在实现了
Pause/Resume(`containerd-shim-v2/service.go:709`/`:750`)。所以驱动对一个运行时
说两种协议。

- `zun/criapi/tasks.proto` **不是** containerd 那份文件的拷贝:只声明需要的四个调用,
  字段号和服务名与上游一致——线格式要一致的就这些。照搬原文件会为了三个只装着
  container id 的请求,拖进 mounts/descriptors/metrics 一整棵类型树。
- ⚠️ **每个调用都要带 `containerd-namespace: k8s.io` 头**
  (`containerd/pkg/namespaces/grpc.go:27`)。不带,containerd 去默认命名空间找,
  对一个明明在跑的容器回"不存在"。

### 4.3.3 两次"服务两种形态"打断了 capsule

都在共享代码上,都只伤新建、不伤在跑的,所以**极易漏过**:

1. **周期状态同步遍历 `unit.containers`**,原生容器没有这个属性 → 抛异常。
   而**一次抛出结束整趟清扫**,于是那台节点上**所有 capsule 也不再被对账**。
   假定 capsule 的方法遇到 container 不是"跳过",是"把整件事拖下水"。
2. **预拉取用 `hasattr(driver, 'pull_image')` 当能力探针**。为 images API 加上
   这个方法,探针就自己翻转了:每个新 capsule 开始预拉它自己的沙箱镜像
   `kubernetes/pause`——那不是一个真镜像。**调用方依赖的能力必须被声明,不能从类
   的形状推断**(现为 `pulls_own_images`,且按对象自己的驱动读)。

### 4.3.4 验收方法:状态不变不是证据

⚠️ **没实现的动作照样回 202**——API 只负责受理,`NotImplementedError` 发生在
计算节点上,调用方看不到。首轮 22 项里有**三项通过却什么都没做**:`reboot` 的
终态和起始态都是 Running、`kill` 之后再没人看过那个容器、`network_detach` 之后
没人看过地址。

**当"成功"和"静默无操作"产生同一个状态时,判据必须去找动作发生过的证据。**

⚠️ **这个陷阱一共咬了四次,后三次都是判据本身写错:**

1. 三项通过却什么都没做(上面那三个)。
2. 用 `started_at` 判 reboot ——那个字段被策略从租户视图里剥掉了,`None -> None`
   把一个正常工作的 reboot 判成无操作。最后用 **marker 文件**才判准
   (顺带证明了重建会丢可写层)。
3. kill 之后等 `Running / Stopped / Exited` ——把 `Running` 也算进"可接受终态",
   于是 wait 在信号落地前就返回,后面每一步都跑在没人检查过的状态上,
   **一个正确的 rebuild 被判成坏的**。
4. ⚠️ **有一轮失败的检查,后面两轮原样重跑全过,中间什么都没改**。这套测试有真实的
   偶发性,**单独一轮全绿不算证据**。查偶发之前,先确认自己看的是不是同一个东西。

**判据错的时候,失败和通过一样没有信息量。**

### 4.3.5 契约测试

按 **zun-ui 实际调用的 19 个容器方法**(`zun_ui/api/client.py`)打,不是 api-ref
的 28 个端点。凭据用租户 **application credential**——admin 令牌会让 `list` 这项
失去意义(Zun 的 DB 层把 admin 上下文当成跨所有项目)。

⚠️ `containers_view.py:64-66` 按 `container:get_one:<字段>` 策略**逐字段剥离**,
所以 api-ref 的响应字段表是 **admin 视图**;租户看不到 `host`/`privileged`
是上游行为。images/hosts 端点的 403 同理(`RULE_ADMIN_API`)。

### 4.4 交互式 exec 的部署形态(已实现)

CRI 的 `Exec` RPC 返回运行时自己流式服务器的 URL,那个服务器**已经**说 kubectl
要的协议(`v4.channel.k8s.io`,五通道:stdin/stdout/stderr/error/resize)。

⚠️ **它监听 `127.0.0.1:随机端口` 且自身无认证**——URL 里的一次性 token 就是全部
凭据。所以**不暴露它**:`zun-wsproxy` 跑在每台计算节点上(它本来就是独立服务),
能到达 loopback,对外只给 token 认证的 websocket。

否决了"让 containerd 对管理网监听":等于每节点开一个无认证的容器入口,绕过 Zun
的 policy,共节点形态不可接受。

⚠️ **CRI 没有 resize RPC**——终端尺寸走流内第五通道,纯字节搬运的 proxy 免费带上。

## 五、共享文件系统的信任边界

**2026-08-11 落成控制。**之前这条只写在文档里,而文档不是控制。

share 挂在节点上,文件服务器按**客户端地址**授权,所以**信任单元是节点**——不是
capsule,也不是租户。只跑 capsule 的节点上,持有节点身份就等于是平台自己;而与
kubelet 或 nova 共存的节点上,它还等于那台机器上任何以 host network 跑着的东西,
**那是别的租户的负载**。

两条控制,都**默认拒绝**:

1. **`[volume] host_dedicated_to_capsules`**(默认 `false`)。节点必须自己声明
   "这里没有别的租户负载",否则**根本不挂 share**。
   ⚠️ **开通/部署流程必须为纯 KNaaS 节点设置它**,否则 RWX 全线不可用。
2. **授权集宽于单机即拒挂**。本驱动发出的每一条授权都是 `/32`、最后一个挂载走了就
   撤销,**那就是宿主挂载 share 的全部隔离**。一条子网规则——手工加的、别的工具加的、
   运维为了解决另一个问题加的——就把它换成了零,而**capsule 看不出任何区别**。
   非 `ip` 类型的规则同样拒绝(那是另一套授权模型,不由我们判定安全)。

**为什么是拒绝不是告警**:被保护的性质(只有平台读得到租户的文件)**从 capsule
内部不可观测**——一个邻居也能读的 share,和一个私有的 share,长得一模一样,直到
它被读走。**拒绝对能修的人可见,暴露对谁都不可见。**

⚠️ **这不能让共节点形态变安全,也不打算。**它让那个形态**不可用**,直到出现
**凭据属于 share 而不是属于节点**的后端(CephFS + cephx,每 share 一把钥匙),
或者**挂载动作进 guest**(客户端身份 = capsule 自己的 OVN port IP,授权单元与租户
边界重合;代价是存储网对租户网可路由的拓扑反转 + guest 内核 NFS client +
Zun→Kata direct-volume 通道,见 DESIGN §8.2 P3)。

## 六、计量与配额

- **配额**:上游现成(`common/quota.py` + `quotas`/`quota_usages` 表 +
  `/v1/quotas`),按 project 记,capsule 与 container 天然同账。
- **计量**:⚠️ **Zun 不发任何通知**——唯一的 notifier 引用在
  `common/exception.py:56-84`,且无调用点传值,是死代码。但**走 ceilometer 不需要
  补通知**:用 central pollster 路径(参照 `ceilometer/load_balancer/octavia.py`
  128 行 + `ceilometer/volume/discovery.py` 58 行),查服务 API 列资源、产出样本,
  全部工作在 ceilometer 侧,Zun 不用动。数据源就是 §3.3 的 capsule stats,而且
  CPU 本来就是累计值,正好对上 ceilometer 的 cumulative 样本类型。

## 七、验证

改了 capsule schema / driver 加载 / CRI 版本,需要证明 API 契约没破:
`zun-tempest-plugin` 的 `test_capsules.py` 是最贴近我们用法的一组。
