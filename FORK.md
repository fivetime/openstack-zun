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

#### 4.3.1h exposed_ports 改为纯声明(2026-09-01)

**上游语义**(api-ref 原文):"Zun will create a security group with a set of rules to open the
ports … and associate the security group to the container"。只有 DockerDriver 实现了:每个带
`exposed_ports` 的容器**新建一个 SG**、每端口一条 `ingress 0.0.0.0/0`(只有 IPv4),删除时删
`security_groups[0]`;CriDriver 一字不读——同一个请求在两种节点上是两个意思。

**为什么不补 CriDriver 而是改契约**:上游这个语义在这个云上没有位置——
- docker 的 `--expose` **本来就只是声明**(`moby/daemon/container_operations.go:116-165`:
  `ExposedPorts` 只进 `OptionExposedPorts`,桥驱动零处消费;`-p`/`-P` 才进 `OptionPortMapping`)。
  租户视角的"机器"上,发布走 DaaS 的 FIP 端口转发,可达性由 `security_groups` 决定。
- 每容器一个 SG 对象踩在最贵的轴上:SG 对象建删触发全云 northd 全量 lflow 重算
  (`northd/en-sync-sb.c:170-173`,上游 main 至今如此),而容器建删是最高频事件。
- 租户默认配额 `secgroups=10`:第 9 个 `--expose` 容器就 OverQuota。
- 生产零个 `zun-*` SG:DaaS 与 kubezun 都不发这个字段,没有存量。

**改法全是删代码**:DockerDriver `_process_exposed_ports` → `_declare_exposed_ports`,只保留
`kwargs['ports']`(docker 自己的声明,节点上 `docker inspect` 可见);删除路径不再碰 SG
(否则删的会是租户后来 `add_security_group` 加上的组);`neutron.py:expose_ports()` 删;
schema 里 `security_groups` 与 `exposed_ports` 的互斥解除(互斥只因自动 SG 会和显式 SG 打架);
api-ref 两处描述改为声明语义;python-zunclient `--expose-port` 文案同步、与 `--security-group`
不再互斥。CriDriver 原样"存起来、show 里回显",即为正确实现。
⚠️ 实测时发现**视图从来没导出过 `exposed_ports`**(上游 1.24 收下就没回显过),
**1.51** 把它加进容器响应——声明语义的另一半。

#### 4.3.1i 释放 docker 网络必须是"最后一台主机"才做(2026-09-01 生产事故)

DockerDriver 的 `_release_networks_left_unused` 在本节点最后一个容器离开时删掉节点上的 docker
网络(好让 kuryr 释放 IPAM 子网池,否则孤儿池累积到 kuryr 拒绝建网)。**但 kuryr 的子网池
`kuryrPool-<cidr>` 是全云一个 neutron 对象,三台节点的 docker 网络共用它**:一台释放 = 池被删,
其它两台的 docker 网络仍持有死掉的池 id,此后那两台上**每一次容器 start 都失败**
(`IpamDriver.RequestAddress: No subnetpools with {'id': …} is found`),直到手工删掉它们的
docker 网络让 kuryr 重建。2026-09-01 生产三台里两台同时中招(container1 持 `449d8673`、
container2 持 `d6a2adaa`,都已不存在;container3 刚重建拿到 `31ed2e55`)。
修法:释放前查 `ZunNetwork` 表,**别的主机还有该 neutron 网络的行就不删**(只删本机的行)。
节点范围的决定不能删全云范围的资源——同一条教训见 kubezun §7.7.5c。
清理悬空网络:节点上 `docker network rm <neutron-net-id>`(须无容器挂着;kata 容器卡死时
`docker rm -f` 会超时,先 `service disable` 该节点)。

**同日第二刀:回收改成周期对账。** 释放原本只在"本节点最后一个容器删除"那一刻发生,删除半途失败、或
neutron 网络被租户删掉时节点上的 docker 网络正好空着,包装就永远留下(container2 上两个 compose 网络的
包装比它们的 neutron 网络多活了两天,各持一个池)。现在 compute 每 `reclaim_stale_networks_interval`
(默认 900s)扫一遍本机记录的 `ZunNetwork` 行,用删除路径同一套判据;并且 **neutron 网络已不存在的,
不管别的主机还包不包着都删**(全是垃圾)。neutron 问不到 = 不删。

#### 4.3.1j 半接线检测:started 必须真的接了线(2026-09-02)

生产事故形态:挣扎中的节点上容器"起来了",docker 说 running、neutron 端口 ACTIVE、SB binding up,
**唯独宿主机上没有那个端口的 tap 接口**——所有状态都在说谎,包全进黑洞,追了两小时。
不说谎的证人是宿主网络命名空间里的接口本身,而 zun-compute(hostNetwork)看得到。

- **start 时**:`_assert_wired` 等 `('tap'+port_id)[:14]`(kuryr 的命名,NIC_NAME_LEN=14)出现,
  超时(`[docker] verify_wiring_timeout`,10s)即停容器、报 `never wired: no host interface …`。
- **运行期**:状态同步循环里 `_watch_wiring`,**连续两轮**缺失才把真话写进 `status_reason` 并记 ERROR
  (保持 Running——docker 对进程没说错,错的是网络)。恢复即清零。
- `[docker] verify_wiring=false` 可关;前提是 zun-compute 共享宿主网络命名空间(chart 就是这么部的)。

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

### 4.5 与官方上游的合并记录

- **2026-08-22**:合入 `e79265e8..c1a74da1`(12 个提交),唯一冲突在 `tests/unit/api/base.py`,取上游版。
- **2026-09-01**:合入 `c1a74da1..09834b79`(44 个提交,hacking 清理 / pecan `_route(args, request)`
  签名 / bindep / tox),零冲突。⚠️ **其中 `2e5a7257 Replace deprecated remotable_classmethod`
  在合并后被 revert**(`f8895ee0`):它把 `@remotable_classmethod` 换成 `@classmethod` + `@remotable`,
  这要求 oslo.versionedobjects 的 `remotable` 认得 classmethod;**stable/2026.1 栈的 o.vo 3.9.0 不认**
  (包装器无条件取 `self._context`,在类上是 `AttributeError: type object 'Capsule' has no attribute
  '_context'`),实测 api 套件 302→209、objects 135→51。`remotable_classmethod` 在 3.9.0 里仍存在,
  只是 deprecated。**o.vo 升到认 classmethod 的版本后,把这个 revert 再 revert 掉即可**;
  在那之前每次合上游都要留意别把它带回来。

### 4.6 运行时档位与 pod 级 cgroup(2026-09-04,gVisor 兼容实测定案)

在测试床验证 gVisor(runsc release-20260817.0,containerd 2.4.0-beta)全链路,
**结论:兼容**——创建/调度(runtimeClassName→capsule.runtime→handler)、OVN 网络
(podIP 不变式、DNS、TCP 双向)、ExecSync、logs、stats(CPU/内存/网络计数)、
重启机制全部实测通过。这次实测同时捞出四个**与 runtime 无关**的存量 bug,均已修:

1. **restart_policy 对 capsule 成员从未生效**(`619a3576`):`_restart_on_exit` 的
   fresh 读用了 `objects.Container.get_by_uuid`,它按 `TYPE_CONTAINER` 过滤,
   capsule 成员是 `TYPE_CAPSULE_CONTAINER` → 永远 `ContainerNotFound` → 每趟静默
   False。**测试当年正是 mock 了这个错误调用才全绿**;现在 mock any-type getter,
   typed getter 设为必炸,回归即红(突变验证 7 红)。
2. **pod 级 cgroup 没人写**(`109163fc` + `823dbf5a`):K8s 里 pod 总限额由 kubelet
   在调运行时**之前**写进 pod cgroup;本链路无 kubelet,该 cgroup 全是 `max`。
   对 gVisor 是致命的:所有容器进程都在 sentry 一个宿主进程里,per-container
   cgroup 是空的,pod cgroup 是限额唯一能咬住的地方——实测 1C/256Mi 的 capsule,
   sentry 按宿主 16C/31G 定容,512MB 分配跑通、4 忙循环烧 4 核。现在 driver 兼任
   kubelet:建沙箱前按成员限额之和(kubelet 同款算法,单成员无限额则该项整体放开)
   预建 pod cgroup 并写 `cpu.max`/`memory.max`/`memory.swap.max=0`,写不进则拒绝
   创建(fail-closed)。**只对 `cri_pod_cgroup_runtimes`(默认 runc,runsc)生效**
   ——kata 在 guest 里执行限额,宿主按成员和截断会饿死 VMM(kubelet 靠
   RuntimeClass overhead 解决,我们没有这个来源)。同时 sandbox 配置补了
   `LinuxPodSandboxConfig.resources`(kubelet 本来就填的字段),kata 的 VM 定容
   注解(`io.kubernetes.cri.sandbox-*`)因此就位;⚠️ **runsc 不读这组注解**,
   它读自己 boot 进的 cgroup——所以 cgroup 必须先于沙箱存在,顺序即语义。
   实测修后:sentry 自视 256Mi/2C,4 忙循环被压在恰好 1.0 CPU(内核 throttle)。
3. **沙箱本体死亡 = 永久卡死**(`94665a44` + 两个跟进):pod cgroup 落地后,超限
   分配会让内核把整个 sentry OOM 杀掉(VMM 崩溃同形);死沙箱拒绝 CreateContainer,
   重启机制每趟撞同一堵墙。现在 sweep 重启前先问沙箱(`PodSandboxStatus`),
   NOTREADY/不存在都算死 → **整 capsule 原位重建**:Neutron port 属于 capsule 不属于
   沙箱,`create_or_update_port` 按 id 复绑,**IP 跨沙箱死亡保持不变**(实测);
   init 容器照 kubelet 语义重跑;全员重启计数 +1。跟进修了两处只有实跑才暴露的坑:
   sweep 的周期 context 无项目,SG 解析查 `tenant_id=None` 报"安全组不存在"
   (重建改用 capsule 属主身份的 context);重建构造的网络请求缺
   `preserve_on_delete` 硬键(从 capsule.addresses 原值回读)。
4. **"runtime 不认识就跳过"遮蔽重启**:半途失败的重建会留下"记录已标死、运行时
   无尸体"的形态,sweep 的保守守卫连重启判定一起跳过,capsule 带着 always 永远
   Stopped。已标死(status_detail 带 `exit:`)的记录即使无尸体也进重启判定,
   锁内 fresh 读保留全部安全检查;未标死的(创建中/运行时恢复中)照旧不动。

**gVisor 的限额语义与 kubelet+runsc 对齐后仍有一点档位差**:内存超限杀的是整个
sentry(pod 死 → 自动重建,分钟级),不是像 runc 那样只杀超限进程(容器级 cgroup
先咬住,pod 活着)。这是 gVisor 结构使然,kubelet 下同样如此,不是我们的缺口。
kubezun 侧配套(`42b5b02`):`PhaseOf` 学会看 restartPolicy——Stopped + Always
不再进 Failed 终态(终态是单行道,capsule 在下面复活了 K8s 也永远报死);
OnFailure 非零退出同理,Never 保持原判。

另:ExecSync 一个已知尖角——exec 的命令若留下持有 stdout 的后台进程,该 ExecSync
永不返回,会占死 zun-compute 对该节点的 RPC 通道(504 compute-node-unresponsive),
直到进程退出。跨 runtime 皆然,列为待办(需要 ExecSync 带 timeout 或改走流式)。

### 4.7 创建失败的 capsule 会把沙箱和端口永远留在节点上(2026-09-08,`f6823d29`)

**怎么发现的**:三台测试床节点上有 15 个 Ready 沙箱,Zun 数据库里只有 6 个 capsule。
逐个 UUID 比对,9 个沙箱 Zun 查无此记录,却都是活着的 kata VM(qemu 进程在、
RSS 1.2–1.8 GB/台),**每个还占着一个 Neutron port 和一个 IP**。node-04 上 5 个全是
孤儿——调度器以为这台是空的。年龄 8 月 16 日到 9 月 2 日,不是一次事故,是稳定泄漏。

**泄漏链(每一环都有日志/代码实证,不是推理)**:
1. 创建在沙箱建好之后失败。`create_capsule` 先 `RunPodSandbox`(此时 port 已建、VM 已起),
   再逐个建成员容器;成员失败(那次是镜像路径的 `ImagesStub.PullImage`),沙箱已在。
2. `_fail_container(..., unset_host=True)` 把 `host` 抹成 None(上游 `bc8375bb`)。
3. API 删除走 `if capsule.host: RPC else: destroy rows`(上游 `69c311a1`):无 host
   就**直接删库,从不通知任何计算节点**;`container` 表无 `deleted` 列,硬删,事后无痕。
   删端口在驱动路径里,自然也没做——这就是端口和沙箱同时留下的原因。
4. 三道兜底网各漏一半:`reclaim_orphan_containers` 默认 `docker_only`,CRI 沙箱不扫;
   `reclaim_orphan_ports` 只收 `DOWN` 的端口,而孤儿沙箱活着端口就一直 `ACTIVE`;
   kubezun 的 `sweepOrphans` 拿 Zun 记录比 pod——**记录没了它看不见**。
   ⚠️ 更深一层:就算把清扫开到 `all` 也收不到——`create_capsule` 建沙箱时**从不打
   owner 标签**(只有 Container API 路径打),而 `reap_orphans` 对无标签的沙箱一律当
   kubelet 的不碰。三道网的盲区正好重叠在同一个洞上。

**定案**(`f6823d29`,四处一起改才成立):
- `create_capsule` 失败时自己收尾:先拆沙箱、再删端口,两步各自独立(一步失败不
  拖累另一步),且都不许替换原始异常——租户要读的是创建为什么失败;收掉的引用
  从对象上清空,存成 ERROR 的行不再指向已不存在的东西。**这是主修,从源头断掉。**
- `_create_pod_sandbox` 给每个沙箱打 `io.zun.container.uuid` 标签(capsule 路径补齐)。
- `reclaim_orphan_ports` 的 `known` 加上 `Capsule.list`——`Container.list` 只回
  `TYPE_CONTAINER` 行,和 9 月 4 日 restart_policy 那个 bug 是同一个坑;不补的话任何
  沙箱临时不在(重建中)超过 10 分钟的 capsule,端口会被当孤儿删掉。
- `reclaim_orphan_containers` 默认改 `all`。原来的 help 说"CRI 路径的权威在 kubezun,
  两个清扫会抢"——前提不成立:kubezun 只能清**有记录的** capsule,`reap_orphans` 只清
  **没记录的**带标签沙箱,集合不相交,抢不起来。这道网现在只管 zun-compute 在建沙箱
  与失败处理之间崩溃这类修不掉的情况。

**验证**:单测 258 绿,红检(把三处修复换回 HEAD)新用例 6/6 转红;测试床活体:
镜像拉不到的 pod → capsule Error、host=None → node-06 上**无该沙箱、Neutron 无该端口**
→ 删 pod 走无 host 分支,记录消失,节点与 Neutron 仍干净;正常 pod 的新沙箱带
owner 标签。清扫:在 node-04 埋一个带标签、无记录的诱饵沙箱,第一轮周期任务收掉
(`[k8s.io] reaped orphan 00000000-dead-beef-… (age 433s): 1 reaped, 0 too young,
0 failed`),同一轮对 6 个有记录的 capsule 沙箱一个没碰。
存量 19 个沙箱(9 活 + 10 死壳)与 9 个端口已手工清理。

⚠️ 顺手记两条判据教训:① `crictl pods` 的 CREATED 列是多词("8 days ago"),按列号
切分必错,用 `-o json`;② `crictl rmp` 默认 2 秒超时对 kata 沙箱不够,报
`DeadlineExceeded` 不是删不掉,加 `--timeout 120s`。

### 4.8 一条 exec 冻住整个 zun-compute(2026-09-08,`0e433fed`/`e5d27e1d`/`cb16b3b6`)

**症状**:`kubectl exec pod -- sh -c "sleep 120 &"`。`sh` 立刻退出,但 `sleep` 继承了
exec 的 stdout 写端,运行时要等这根管子关闭才回 ExecSync——请求里的 `timeout` 管的是
杀命令,管不到这段等待。实测:这条 ExecSync 挂了整整 120 秒,而**同一时段 zun-compute
的日志一行都没有**——没有周期任务、没有心跳、没有别的请求(另一条 exec 60 秒后 504
`compute-node-unresponsive`,第三个 capsule 上的 exec 等了 53 秒才回)。

**根因有两层**,分别修:

1. **gRPC 调用阻塞在 C 里,eventlet hub 拿不回 CPU**。zun-compute 是 eventlet 进程,
   `runtime_stub.ExecSync()` 这种阻塞式 unary 调用在 `cygrpc` 的 `next_event()` 里
   等,整个 OS 线程停住,所有绿色线程一起停。这不只是 exec:**每一次慢的 CRI 调用
   都在冻进程**——PullImage 拉一个大镜像的几分钟里,这台节点心跳全停(`service_down_time`
   180 秒,拉够久就被判 down)。修法照 nova 对 libvirt 的做法:四个 unary stub
   (runtime/image/task/snapshot)包一层 `eventlet.tpool.Proxy`,调用跑在原生线程池,
   hub 继续转。commit 用的三个流式 stub(content/diff/ctrd_image)**不包**——Proxy 回来的
   是迭代器,每 next 一次又阻塞 hub,换个脸的同一个问题(Container API commit 路径,
   不在 capsule 主干,留待)。
2. **包了 tpool 之后当场死锁**(第一版 `0e433fed` 部署到 node-06 一分钟内服务无响应,
   py-spy:20 个 tpool 工作线程全部停在各自私有 hub 的 `hub.run` 里)。eventlet 的
   monkey patch 把 `threading` 模块的 Lock/Condition 换成绿色版,而 gRPC 用它们保护
   channel 状态(`cygrpc._ChannelState.condition`)。绿色锁只在**同一个 hub 的 greenlet 之间**
   成立;两个原生线程争同一把,输的一方要 switch 到另一个线程上的 greenlet——
   `greenlet.error: Cannot switch to a different thread`——然后永远等不到唤醒。
   压力实验坐实:40 个并发调用方经 tpool 打 720 次调用(含 ExecSync),monkey-patched
   状态下 60 秒**0 个完成**;把 `grpc._channel`/`grpc._utilities`/`cygrpc` 三个模块的
   `threading` 全局改绑回 `eventlet.patcher.original('threading')` 后 **2.0 秒全部完成**。
   gRPC 本来就不向 hub 让步(等待都在 C 里),绿色原语对它一无所用。落在
   `_grpc_on_native_threads()`,driver 导入时执行一次,单测钉住三个模块
   (`test_exec_hold.py::NativeThreadingTest`)。⚠️ 为什么 node-04/05 没死锁而 06 死锁:
   04 零 capsule、05 两个,并发量到不了争锁;06 六个 capsule 的探针+stats 一开就撞上。
   **"两台正常"不是"代码正常"的证据,得看最忙的那台。**

**第三层:让那条 exec 本身也有界。** ExecSync 加客户端 deadline = 命令 timeout +
`EXEC_REPLY_MARGIN`(15 秒),整体压在 `rpc_response_timeout`(60)以下,API 回的是原因不是
504。containerd 侧还有 `drain_exec_sync_io_timeout`(节点 drop-in
`70-exec-drain.toml` = 5s),运行时自己放弃等管子,回 "io is still held by other
processes",driver 把它翻成"命令已结束但它起的进程还占着输出,后台进程请重定向输出"。
⚠️ **这个 drain 对 kata 无效**——查过 3.31.0 源码:shim 的 `wait()` 对 exec 先
`<-execs.exitIOch` 再 `WaitProcess`(`containerd-shim-v2/wait.go:39`),**退出事件本身
就等 IO 关闭**,containerd 的 drain 根本轮不到。实测:runc 沙箱 15 秒回 drain 错误
(5 秒 drain + 10 秒 kill 等待);kata 沙箱一直沉默到子进程退出,由我们的 deadline 在
45 秒收口。所以 deadline 触发时的文案两种原因都点(命令超时 / 输出被占),并给出
重定向的办法——kata 档租户看到的就是这条。

**验证**(三台 `cb16b3b6` + drop-in):holder exec 45.4 秒回 400 带新文案;它挂着的时候
同节点另一个 capsule 的 exec **0.8 秒**回(修前 53–60 秒);node-05/06 每 60 秒 71 条
周期任务、零 greenlet 错误;控制组 `echo hi; sleep 2; echo done` 2.8 秒输出完整。

### 4.9 一个卡死的 shim 堵死整台节点的 CRI 通道(2026-09-14,`74a53df9`)

**怎么发现的**:测试床健康巡检。node-05/06 两台 VM 在 09:49 同时出现卡在
`kvm_async_pf_task_wait_schedule` 的 D 状态进程——客户机向 incus 宿主机要一页内存,
宿主机再没回"页已就绪"(内核 09:52 起报 hung task,10 秒复查 CPU 计数纹丝不动)。触发点
是两个经 Container API 新建的 kata-qemu 容器(`q3`/`qdef`,admin 身份,非本会话所建);
宿主机侧根因不在本仓库。node-05 上被卡住的恰是 zun-compute 主线程本身(判 down,不可
kill);node-06 上卡住的是 `q3` 的 shim 和另一个 capsule 共用 shim 里的一个线程。

**我们自己的缺陷在 node-06 暴露**:心跳正常、服务 `up`,但 111111/222222 四个 CoreDNS
capsule 的 exec/logs **全部 60 秒后 504**。py-spy:tpool 20 个工作线程里 **19 个卡在
`ListContainerStats`**、1 个卡在删除 `q3` 的 `StopPodSandbox`。`ListContainerStats` 是
节点级调用,containerd 挨个问 task,一个 task 不答整条不返回;kubezun 的指标抓取每轮
都发一条新的,**gRPC 不给就没有 deadline**,每条都带走一个线程,池满后所有 CRI 调用排队。
对照组坐实这是我们的问题不是 containerd 的:直接 crictl 问,**同节点 3 个健康 capsule
的 stats ~1 秒、exec 160 毫秒**,只有真坏的两个超时。§4.8 把调用挪上 tpool 保住了 hub,
却把"冻整个进程"换成了"心跳活着、CRI 全堵"——**更难从外面看出来**。

**修法**:四个 unary stub 外面再包一层 `_Bounded`,每个调用没传 `timeout` 就按类别补:
读(status/list/stats/images、containerd task Get、snapshot Mounts)`cri_read_timeout`
15 秒——读得频繁,短才不会在抓取周期里叠满池子;改动(run/stop/remove sandbox、
create/start/stop/remove container、update、task kill/pause)`cri_request_timeout`
120 秒,**与 kubelet `--runtime-request-timeout` 默认值一致**,`StopContainer`/`ExecSync`
再加上请求自带的 timeout(优雅期/命令时限);`PullImage` `cri_pull_timeout` 1800 秒;
两张表都没列的方法按 120 秒兜底——**漏列只会变长,不会变成无界**。调用方显式给的保留。

**验证**(故障现场未动,node-06 仍有卡死的 shim):部署后健康 capsule 的 exec **0.8 秒**
(修前 60 秒/504),logs 0.8 秒;与卡死 shim 同沙箱的那个 capsule exec 2.8 秒报错返回;
5 分钟观察窗每 30 秒采样 10 次:tpool 在途 CRI 调用 **6–9 个**(上限 20,修前 20/20 满),健康 capsule exec **740–822 毫秒**;在同一台节点上建/exec/删一个新 pod:
19 秒 Running、exec 0.8 秒、删除 30 秒,删后沙箱/端口/capsule 记录零残留。
单测 287 绿,红检新增 10 个用例在修前全红。

⚠️ 遗留(未做,有意):两个节点级统计(`measure_writable_layers`/`sample_counters`
的 `ListContainerStats`、`_sandbox_networks` 的 `ListPodSandboxStats`)仍是"一个坏沙箱 →
整台节点这一轮没有数据"。现在它们会在 15 秒后放手而不是永远挂着,但计费采样那一轮会
整台丢;要做到"坏沙箱只丢自己"需超时后退回逐容器 `ContainerStats`,这改变了
`test_sample_counters` 里"整台不答就一条不报"的既定语义,单独决定。

### 4.10 CNI 插件不能依赖宿主机上的 Python(2026-09-15,`427ea54a`)

**背景**:capsule 的网卡由 CNI 插件 `zun-cni` 接入 Neutron,插件由 containerd 在**宿主机**上执行。原插件
`zun.cni.cmd.cni` 是 Python console script:向 daemon 要 VIF(`/addNetwork`),自己转成 CNI 结果。测试床的
daemon 和 venv 都在宿主机上,所以一直能用;生产用 openstack-helm 部署,daemon 跑在容器里,镜像里的插件拷到
宿主机后 shebang 指向不存在的解释器——生产 capsule 从未能建成网络(此前还因 containerd 读 kubelet 的 CNI 目录,
沙箱落到 multus)。

**定案**:把"VIF → CNI 结果"挪进 daemon,宿主机只放一个转发器。
- daemon 新增 `/cni`:收同样的请求(`CNI_*` + `config_zun`),整次调用在 daemon 内完成,回 CNI 原生格式——ADD
  回结果、DEL 回空、VERSION 回 supportedVersions(不解析 CNI_ARGS,runtime 发 VERSION 时不带)、失败回带码的
  CNI 错误(端口激活超时保留 timeout 码;DEL 遇 ResourceNotReady 仍成功,同 `/delNetwork`)。转换函数抽成
  `vif_to_cni_result()`,Python 插件复用,旧端点不变。
- `tools/zun-cni-shim`:Go 标准库、静态;收集 `CNI_*` 与 stdin 配置转发到配置里 `zun_cni_daemon`(默认
  `http://127.0.0.1:9036`),打印回包;VERSION 本地答;daemon 不可达或回非 CNI 错误体时仍输出 CNI 错误并非零退出,
  **绝不空成功**(chart 旧脚本在找不到插件时会生成转发给 bridge 插件的替身——静默接到 Linux bridge)。

**验证**:单测 endpoint 11 个(修前 9 红,剩下 1 个钉住旧端点不变)、shim 8 个。测试床 node-04 用 shim 顶替
Python 插件跑租户 pod,ADD/DEL 均 `/cni 200`,port ACTIVE 绑本机、删除后 port/沙箱/br-int 接口清零,与 Python 插件
对照组一致。生产(zun-worker1~3,镜像 + chart + containerd drop-in,部署细节在平台文档
OpenStack-Helm-Deploy.md §14.2.10):5 个 kata-qemu capsule 分布三台,IP == port IP,9 向跨节点 ping 0%,
删除 15s 清零。

⚠️ 上生产才暴露、测试床暴露不了的两处(都因测试床 daemon 在宿主机上):① 插口经 privsep 调 **`ovs-vsctl`**
(`zun/network/linux_net.py`),zun 镜像原本没有——镜像 bindep 补 openvswitch-switch;② os-vif 拔 tap 用它自己的
privsep 上下文 `vif_plug_ovs_privileged`,不配 helper_command 就回退 `sudo zun-rootwrap`,容器里没有,DEL 失败、
capsule 删除卡住——部署侧配 helper_command。**测试床验证的是代码,不是镜像与 chart**。

### 4.11 API 1.53:docker 的一批创建选项(2026-09-17)

DaaS 网关按名拒绝了一串 docker 选项,理由都是"zun 没有字段"。这一版一次补上,全部是创建时字段,
都可为空(空 = 运行时默认,与之前一样)。迁移 `e1f2a3b4c5d6`;对象 Container 1.52、三个 capsule 类 1.10。

| 字段 | 形状 | DockerDriver | CriDriver |
|---|---|---|---|
| `extra_hosts` | `["name:addr", ...]`,≤64,地址可以是 IPv6 | `extra_hosts` | 拒绝(CRI 没有 hosts 条目) |
| `dns_options` | `["ndots:2", "edns0"]`,≤16 | 有自定义 resolv.conf 时写进文件最后一行(覆盖我们的 `ndots:1`),否则 `dns_opt` | 沙箱 `dns_config.options` |
| `ulimits` | `[{"name","soft","hard"}]`,名字限 15 种,-1 = 不限 | `docker.types.Ulimit` | 拒绝 |
| `shm_size` | 整数 **MiB**(与 `memory` 同单位),1..65536 | `shm_size` 字节 | 拒绝 |
| `read_only` | 布尔,**根文件系统**(挂载的只读在 1.52 的 `mounts[].read_only`) | `read_only` | `readonly_rootfs` |
| `init` | 布尔 | `init`(false 也显式传) | true 拒绝,false 放行 |
| `group_add` | `["audio", "1001"]`,≤32 | `group_add`,排在安全上下文加的组之后 | 只收数字 → `supplemental_groups`;名字拒绝(同 `user` 的组名) |
| `oom_score_adj` | -1000..1000 | `oom_score_adj` | `resources.oom_score_adj` |
| `tmpfs` | `{"/path": "rw,size=64m"}`,≤16,路径须以 `/` 开头 | `tmpfs` | 拒绝 |
| `healthcheck.start_period` | 秒 | 纳秒传给 docker | 不适用(CriDriver 不执行命令式健康检查) |
| `healthcheck.disable` | 布尔,不能与 `cmd` 同给 | `Test: ["NONE"]` | 天然成立(同上) |

- **kata 下各自落在哪**:`extra_hosts`/`init`/`group_add`/`ulimits`/`read_only`/`tmpfs` 由 guest 里的
  agent 照做,容器看到的就是它要的;`shm_size` 是 guest 里 `/dev/shm` 的大小,吃的是 VM 的内存;
  `oom_score_adj` 写给 guest 里的进程,宿主看到的是 shim,宿主 OOM 杀谁仍由 VM 的内存上限决定。
- CriDriver 的拒绝发生在建沙箱**之前**(kata 下建了沙箱就是一台 VM)。
- 两个驱动都按**类型**判断字段是否被要求:不带这个字段的对象(测试桩、老的 capsule 成员)什么都不要。
- 顺带修了 `healthcheck` 的 schema:原先成员写在 `items` 下,而 `items` 只对数组生效,对象上什么都没校验。
  现在在 `properties` 下;其他键仍放行(capsule 路径把探针与安全上下文存在同一列)。
- 同 1.49 以来的惯例,新字段不按请求版本门控;1.53 用来让调用方判断服务端有没有这批字段。

### 4.12 API 1.54:动作上的 docker 选项与内容卷属性(2026-09-17)

| 接口 | 新参数 | DockerDriver | CriDriver |
|---|---|---|---|
| create | `environment` 的值可为 `null`(docker 的 `-e NAME`:去掉镜像里的这个变量) | docker-py 写成不带 `=` 的 `NAME` | 拒绝(CRI 只能设变量不能删) |
| create `mounts[]`(仅 `type: bind` 的内容卷) | `mode`(0..0o7777)、`uid`、`gid` | 节点上写文件后 chmod/chown(`Local.attach`,两驱动共用) | 同左 |
| create | `[volume] max_contents_size`(默认 1 MiB,按解码后字节) | 超限 400 | 同左 |
| stop | `signal` | Engine API `POST /containers/{id}/stop?signal=&t=`(docker-py 不支持,直接发) | 经 containerd 任务服务发信号,等 timeout,退了就 `StopContainer(0)`,没退再按 timeout 停 |
| execute | `detach`(须 `run=true` 且非交互) | `exec_start(detach=True)`,立即返回 exec_id,无输出与退出码 | 拒绝(exec 在一次调用里跑完) |
| put_archive | `copy_uidgid`、`no_overwrite_dir_non_dir` | Engine API 的 `copyUIDGID`/`noOverwriteDirNonDir`(直接发) | 拒绝(容器内 tar 做不到) |
| commit(仅推到仓库的路径) | `message`、`author`、`changes`(换行分隔的 CMD/ENTRYPOINT/ENV/EXPOSE/LABEL/STOPSIGNAL/USER/VOLUME/WORKDIR)、`pause` | docker-py 的同名参数;暂停由 manager 做,驱动总传 `pause=False` | 写进镜像 config 与 history(`commit.apply_changes`),非法指令在驱动里先拒 |

- **RPC 兼容**:新参数只在用到时才放进 RPC 消息,老 compute 节点照常接受旧形状的调用(`rpcapi` 里 `extra=...`)。
- 走 Glance 的 commit 路径带任何选项都拒绝(那条路只上传快照)。
- `pause=false` 时 manager 不暂停,并显式告诉驱动,否则 docker 自己的默认会再暂停一次。
- `exec -i`(有标准输入无终端)**不在这一版**:docker 运行时下 zun 没有可流式交互的 exec 会话,这是结构性的。
- 迁移 `f2a3b4c5d6e7`(volume_mapping 三列);VolumeMapping 对象 1.8。`environment` 改 `DictOfNullableStringsField` 不改指纹,容器对象版本不变。
- ⚠️ **上线要滚四个组件**:db-sync 之后 `zun-api`、`zun-compute`、**`zun-wsproxy`、`zun-cni-daemon`**——后两个也加载 Container 对象,
  旧代码读到值为 null 的环境变量就抛 `Field 'environment[X]' cannot be None`。2026-09-17 生产只滚了前两个:
  `docker run -e NAME` 退出 0 但 attach 输出全丢(wsproxy 加载失败),一台节点的 cni-daemon 列容器也报了 6 次。
- **生产验证(DaaS 第 26 轮)**:`-e NAME`/`--unsetenv`/`--unsetenv-all` 容器里变量确实不在;`--stop-signal`、`stop/restart --signal`
  由容器里的 trap 收到(USR1/INT/USR2/TERM);`exec -d` 3 秒返回、命令 6 秒后落盘;`cp -a` 属主 1000:1000、不带 -a 是 0:0,
  podman 默认 1000、`-a=false` 0;commit 的 CMD/ENV/USER/LABEL/author 进了 Harbor 记录的 config,message 进了 history,
  跑出来的镜像输出改过的 CMD;`--change RUN` 被拒;secret `uid=1000,gid=1000,mode=0400` 容器里是 `400 1000:1000`,
  默认 `444 0:0`;kube `defaultMode: 0400`/items `mode: 0444`/无 mode 分别是 400/444/644。

### 4.13 只给 entrypoint 时不再补镜像的 CMD(2026-09-18)

DockerDriver 建容器前,只要 entrypoint 或 command 有一个没给,就把两个都从镜像补上。docker 自己的规则
(moby `daemon/commit.go` 的 `merge()`)是:**请求给了 entrypoint,就不用镜像的 CMD**。于是只给 entrypoint
的容器被塞了镜像 CMD 当参数——生产实测 `docker run --entrypoint echo nginx:alpine` 打印
`nginx -g daemon off;`,真 docker 打印空行;k8s 的 `command:` 不带 `args:`(经 DaaS `kube play`)同样中招,
而 k8s 的语义与 docker 此处一致(给了 command 就忽略镜像 CMD)。

改为 `_merge_image_command()`:没给 entrypoint 才读镜像,补 entrypoint,并在没给 command 时补 CMD;
给了 entrypoint 就原样下发,不读镜像。不改 API、不改对象、不用迁移,只需重建 zun-compute。
测试 `zun/tests/unit/container/docker/test_image_command.py`(四种组合+镜像两者皆无)。
走 Glance 的建容器路径本来就不补,不受影响。

### 4.14 带 `%` 的错误信息不再变成 500(2026-09-18)

`ZunException` 总是拿消息做 `message % kwargs`(kwargs 至少有 `code`),且只兜 `KeyError`。调用方传进来的
现成消息一旦含字面 `%`——docker 错误里 URL 编码的路径 `%2Fno%2Fsuch` 就是——格式化抛 `TypeError`,外层
`translate_exception` 再包成 500 "Unexpected error: must be real number, not dict"。生产表现:`docker cp`
从容器里不存在的路径往外拷,驱动已把 docker 404 转成 `Invalid`(400),租户却看到 Internal Server Error。
改为:只有带了 `code` 以外的格式化参数才格式化(类模板照旧),兜底加 `TypeError`/`ValueError`。
经 RPC 反序列化(oslo.messaging 用原文+kwargs 重建)同样保持原文。测试
`zun/tests/unit/common/test_exception.py::TestMessagesWithPercentSigns`,旧代码上 4 条失败、报的正是生产那句。

### 4.15 API 1.57:exec 按请求给超时(2026-10-25)

`execute?run=true` 原来只能等 `[docker] execute_timeout`(生产 30 s)或 `[container_driver] cri_exec_timeout`
(默认 30 s),而且这个值必须小于全局 `rpc_response_timeout`(默认 60 s):命令比 RPC 活得久,调用方拿到的是
"节点不应答"而不是超时本身。跑几分钟的命令(qinling 的 Zun 编排后端按函数超时同步调用)只能放弃同步、改轮询。

- 新参数 `timeout`(秒),1.57 起;只对"运行并等待"的命令有效,和 `detach`、`interactive`、`run=false` 同用都是 400。
- 上限 `[api] max_execute_timeout`(默认 300,运维可调);超出 400 并说出上限。请求在命令跑完前一直占着一个
  zun-api 工作线程,这个上限就是"一个调用方最多能占一个线程多久"。
- 带了 `timeout` 时 RPC 走 `_call_bounded`,截止 = `timeout` + `rpcapi.EXEC_REPLY_MARGIN`(30 s)。余量必须大于
  CRI 驱动自己在命令超时后的等待(`cri/driver.py` 的 `EXEC_REPLY_MARGIN`,15 s,运行时杀命令再排空输出),
  否则两边同时到点,调用方听到的是节点不应答;测试 `test_the_margin_outlasts_the_node_s_own` 钉住这个关系。
- 不带 `timeout` 时一切照旧:RPC 消息里不放这个参数,manager 也不往驱动传,老 compute 节点和老驱动照常接受。
  ⚠️ 反过来,带 `timeout` 的请求发到还没升级的 compute 会失败,所以**先升 compute 再升 API**(或一起升)。
- docker 驱动的超时信息现在说出等了多少秒(仍是 409 Conflict);CRI 驱动仍是 400(`Command did not finish
  within N seconds`)。两种状态码不一致是原有行为,这一版没动。
- 测试 `zun/tests/unit/api/controllers/v1/test_exec_timeout.py`、`zun/tests/unit/compute/test_exec_timeout.py`,
  旧代码上 17 条里 13 条失败(通过的 4 条正是"不带时和以前一样")。
- ⚠️ 849daed6 上线(rev 59)后实测:带 `timeout` 的请求全部 500(`_call_bounded() got multiple values for argument
  'timeout'`)。`_call_bounded` 自己的截止参数也叫 `timeout`,和 RPC 参数撞名;单测把 `_call_bounded` 整个 mock 掉,
  没测出来。改名 `rpc_timeout`,补 `test_through_the_real_bounded_call`(走真实 `_call_bounded`,只 mock RPC client;
  旧代码上复现同一个 TypeError)。

### 4.16 按 digest 引用的镜像按 digest 拉取和运行(2026-10-26)

`image` 写成 `repo@sha256:…` 时,`parse_image_name` 只取 name 和 tag,digest 被丢掉、tag 补成 `latest`:docker 路径
拉的是 `repo:latest`。仓库里没有 `latest` 时报 404 `manifest unknown`(qinling 按 digest 钉运行时,生产实测撞上);
有 `latest` 时更糟,跑的是另一个镜像。胶囊走 CRI 驱动,直接用原始引用,不受影响。

- `parse_image_name` 有 digest 时把它放在 tag 的位置返回(同时给了 tag 也以 digest 为准)。docker 的
  `/images/create` 接受 `tag=sha256:…`。
- 新增 `utils.image_reference(repo, tag)`:tag 里有 `:` 就是 digest,用 `@` 拼回去,否则用 `:`。docker 镜像驱动的本地查找、
  docker 驱动建容器(含胶囊里的容器)、CRI 驱动的 `pull_image`、`image_pull` 的 inspect,原来各自用 `:` 拼,都换成它。
- `image_pull`(images API)按 digest 拉到的镜像没有 `RepoTags`,原来取 `RepoTags[0]` 会 IndexError;现在没有就保留请求时的 repo/tag。
- 拉取策略:digest 不是 `latest`,默认 `ifnotpresent`,和固定 tag 一样;digest 不可变,本地有就是同一个镜像。
- 测试 `zun/tests/unit/image/docker/test_image_digest.py` 从引用字符串经 `parse_image_name` 一直走到 docker 客户端
  (只 mock 客户端),旧代码上 6 条里 5 条失败(通过的那条是"tag 和以前一样")。

### 4.17 API 1.58:同步 exec 的输出有上限、stdout/stderr 分开、截断看得见(2026-10-27)

`execute?run=true`(以及胶囊的 `execute`)把命令的全部输出读进内存,经 RPC 原样带回 API。没有任何上限:
一条 `cat` 大文件就能把 compute 节点和 API worker 撑大到文件那么大。另外输出不是 UTF-8 时(二进制、图片),
compute 回 RPC 前做 JSON 序列化直接 `UnicodeDecodeError`,调用方只拿到 500。

- 新选项 `[compute] exec_output_limit`(默认 65536 字节,最小 1024,最大 2 MiB):stdout、stderr 各保留这么多。命令照常跑完、
  退出码是真的,超出的部分边读边丢。docker 驱动的 `read_exec_output` 改成边收边拆帧(以前是全部收齐再拆),
  任何时刻只持有已保留的部分加不到一个帧头。
- CRI 驱动:containerd 的 `ExecSync` 自己攒满整段输出再一次性返回(每路最多 16 MiB,超出静默截断,
  `maxStreamSize`),所以只能在收到后截;某一路恰好 16 MiB 也按"已截断"标记。gRPC 通道的
  `max_receive_message_length` 从默认 4 MiB 调到 33 MiB:以前两路合计超过 4 MiB 时 `ExecSync` 直接
  `RESOURCE_EXHAUSTED`,走不到任何截断逻辑(kubezun 会话指出)。这个通道探针也在用,只调大接收上限,探针行为不变;
  截断只加在 `execute_run`(API 发起的同步执行)上,探针走的 `_exec_in_container` 没动。
- ⚠ **RPC 上传的一律是 base64**(`ExecOutput.as_wire`:`stdout_b64`、`stderr_b64`、`output_b64`、`output_truncated`),
  调用方看到的字段由 API 侧的 `exec_output.client_fields` 生成(`compute/api.py` 的 `container_exec`)。第一版在 compute
  就把输出转成文本放进回复,kubezun 会话核对时实测指出:RPC 载荷是 `ensure_ascii` 的 JSON,oslo.messaging 还要再包一层 JSON,
  NUL 或中文一个字节在消息里要 7 个字节,回复里同一份内容又出现两次(各流 + 合并),上限 L 时最坏约 28L,
  1 MiB 就是 28 MiB,超过 RabbitMQ 16 MiB 的单条上限:不是截断,而是 compute 发不出回复、API 等到超时报 500。
  base64 不管内容是什么都是 4/3,最坏约 5.33L:1 MiB → 5.33 MiB、2 MiB(选项上限)→ 10.67 MiB。测试
  `test_the_largest_reply_fits_the_broker_s_message` 用真实的序列化器和 `serialize_msg` 包装钉住这一点。
  ⚠ 滚动升级时:新 compute 配旧 API,旧 API 会把 `*_b64` 原样转给调用方、没有 `output`。**先升 API 或一起升**。
- CRI 驱动收到 `RESOURCE_EXHAUSTED`(containerd 自己的 `max_send_message_size` 默认也是 16 MiB,超过就拒发)时
  给一句清楚的 400,不再是 500。"≥ 16 MiB 标记截断"只有节点放大了这个发送上限才会走到。
- 新模块 `zun/container/exec_output.py` 的 `ExecOutput`:两路分别保留,记住写入顺序,`combined` 按原顺序交错
  (老字段 `output` 和以前一样;CRI 仍是 stdout 在前、stderr 在后)。
- 回复新增字段(按本 fork 惯例对所有版本都返回,1.58 只记录出现的版本):`stdout`、`stderr`、
  `stdout_encoding`/`stderr_encoding`(`utf-8`,或不是 UTF-8 时 `base64`,原样保真)、`output_truncated`
  (被截断的流)、`output_replaced`(不是 UTF-8 的流)。截断切在多字节字符中间时丢掉那半个字符,仍按文本给。
  `output` 必须是字符串,里面的非 UTF-8 字节换成替换字符;被截断的流在合并输出里同样丢掉末尾半个字符,`output` 和该流字段一致。
- 响应头 `X-Zun-Output-Truncated`、`X-Zun-Output-Replaced`(值为流名,逗号分隔)对任何版本都给:固定在老版本、
  只读 `output` 的调用方(kubezun 固定 1.40)也能知道输出被截断或改过。交互式 exec(`run=false&interactive=true`)
  回复和以前完全一样,不带这两个头。
- 驱动仍可只返回一整块输出(老接口),manager 用 `ExecOutput.from_combined` 包一下。
- **不做的**:不存任何执行结果(用户定:存储的代价不可评估,恶意调用能打爆存储)。不一直占着 API 的做法是
  流式执行(非终端 exec 也走 wsproxy 多通道),另做。
- 测试 `zun/tests/unit/compute/test_exec_output.py`、`zun/tests/unit/api/controllers/v1/test_exec_output.py`、
  `zun/tests/unit/container/docker/test_utils.py`(`TestReadExecOutput`):只把新模块放进旧代码(69a42c84)时,32 条里 18 条失败
  (通过的是新模块自身、交互式不变、旧 compute 回复照传这类);在第一版 0780a601 上 4 条失败(最坏回复超 16 MiB、
  经真实序列化后二进制和整块输出、CRI 拒发)。compute 那组走真实的 RPC 序列化器:旧代码上二进制输出在这里抛
  `UnicodeDecodeError`。读取测试的写入端加了超时:读取方提前出错时,写入线程不会卡在满的 socket 上让清理永远等。

### 4.18 docker 路径 exec 超时后真正杀掉命令(2026-10-27)

docker 驱动的同步 exec 超时(`[docker] execute_timeout` 或 1.57 的 `timeout`)只是不再读、回 409,命令在容器里继续跑:
docker 没有停止 exec 的接口。qinling 的 Zun 后端一个容器一次只跑一个执行,超时没杀掉的命令会和下一个执行抢 CPU 和内存。

- `DockerDriver._kill_exec`:超时后 `exec_inspect` 还在跑,就 `ctr --namespace moby tasks kill --exec-id <exec id> -s SIGKILL <容器 id>`。
  docker 把每个 exec 作为容器 task 的一个进程、以 exec 自己的 id 交给 containerd(moby 命名空间),kill 走 shim,kata 虚机里的进程也能杀到。
  生产实测(zun-worker1,kata-qemu):`detach` 起的 `sleep 300` 被杀,exec 退出码 137,容器主进程不受影响。
- 尽力而为:杀不掉只记 WARNING,调用方照样拿到超时。`ctr` 的用法与孤儿清扫相同(`run_as_root`)。
- CRI 驱动不受影响:`ExecSync` 的超时由运行时自己杀命令。
- 测试 `zun/tests/unit/compute/test_exec_timeout.py`(3 条,mock 的是 `utils.execute` 和 docker 客户端这两个边界),旧代码上
  "超时后会杀"失败;`test_docker_driver.test_execute_run_timeout` 原断言"超时后不 inspect",改为只 inspect 一次。

### 4.19 节点上的错误不再把 traceback 返回给调用方(2026-10-27)

compute 节点上抛出的异常经 oslo.messaging 回到 API 时,`deserialize_remote_exception` 把节点的 traceback 拼进了异常消息。
`NoExceptionTracebackHook` 本来就是为了切掉它(注释写明是安全问题),但只切 `title`;错误中间件(`parsable_error`)把
`description` 原样作为 `detail` 返回,于是每个来自节点的错误(exec 超时 409、运行时拒发 400 等)都把节点的文件路径和源码行
带给了租户。生产实测(rev 64,`timeout=2` 的 `sleep 30`):`detail` 1927 字节,大部分是 traceback。kubezun 会话在测试床发现。

- hook 对 `title` 和 `description` 都切。`CONF.debug` 时保留的规则不变。
- 测试 `zun/tests/unit/api/controllers/v1/test_remote_traceback.py`:用 oslo.messaging 真实的 `serialize_remote_exception` /
  `deserialize_remote_exception` 造出节点回来的异常,只 mock RPC 这一层,走完整 API 栈;旧 hook 上失败(detail 带 traceback)。

### 4.20 API 1.59:流式 exec——API 立即返回,命令输出经 wsproxy 边产生边转发(2026-10-27)

同步 exec(1.57/1.58)让 API 线程一直等到命令结束,qinling 的函数执行和 kubezun 的 `kubectl exec -i`/`kubectl cp` 都被它
卡住;而把结果存进数据库或缓存再让客户端来取,用户否决了(输出可能是任意二进制,体积不可控,"代价不可评估")。所以改成:
**Zun 不存任何东西,输出边产生边转发,有背压**。

- **发起**:`execute?run=true&stream=true[&stdin=true][&timeout=N]`(容器与胶囊都有;胶囊的 timeout 只随 stream 提供)。
  compute 只**建**不**启动**命令(docker `exec_create`,CRI `Exec` 拿流 URL,都不带 tty,stdout/stderr 分开),API 立即回
  `{exec_id, proxy_url, protocol: v5.channel.k8s.io, output: null, exit_code: null}`。`stream` 不能与 interactive/detach/run=false
  同用,`stdin` 必须随 `stream`。
- **令牌**(`zun/websocket/exec_token.py`):Fernet 加密 + 签名,`[websocket_proxy] exec_token_key`(API 与 wsproxy 共用,未设
  则流式被拒 409)、`exec_token_ttl`(默认 30 s)。里面是容器 uuid、节点、运行时目标(CRI 的 loopback URL / dockerd 地址与 exec id)、
  stdin、timeout。**不写数据库**;只能用一次靠运行时(docker exec 只能 start 一次,CRI 流 URL 一次性)。加密而不只是签名:
  运行时的内部地址不给调用方看。
- **协议**(`zun/websocket/channel.py`):Kubernetes 通道协议,每条 websocket 消息 = 1 字节通道号 + 数据;0 stdin、1 stdout、
  2 stderr、3 结束状态、255 关闭(`[255,0]` = stdin EOF,v5 才有)。结束时通道 3 发 `metav1.Status`:Success /
  NonZeroExitCode + ExitCode cause(同 kubelet)/ **Timeout(本 API 自定义)**。
- **握手**(`websocketproxy.validate_connection`):wsproxy **先拨通运行时再回 101**,所以 101 里的 `X-Zun-Exec-Protocol`
  是实际协商结果(v5 = 能关 stdin);客户端按名字要 v5/v4.channel.k8s.io 子协议时也按实际结果选,否则仍是 websockify 的
  `binary`。令牌无效/过期/别的容器的 → 403,运行时拒绝/拨不通 → 502,body 一句原因,不含地址,不在日志里打 traceback。
- **CRI(胶囊)**:原样转发运行时的帧,向运行时先提 v5、只给 v4 时退回 v4;v4 下客户端发 `[255,…]` 立即回 BadRequest 并结束
  (转过去会被忽略,命令永远等不到 EOF)。stdin 由单独线程写,命令不读 stdin 也不会堵住输出。客户端断开或超时 → 硬断后端连接,
  运行时随之结束命令(fork containerd main.20261027 保证;实测上游 2.2.2 对硬断也会结束)。
- **docker**:wsproxy 用节点 dockerd 的远程地址(生产即 mTLS 2376)`exec_start` 劫持连接,把 8 字节头的复用流翻译成通道帧,
  stdin 原样写入,`[255,0]` → 半关闭写方向(经 dup 的 fd,TLS 状态不丢;dockerd 对 exec 设了 CloseStdin);结束后
  `exec_inspect` 取退出码。dockerd 不会因连接断开而结束 exec,所以**超时或客户端断开时 wsproxy cast `container_exec_kill`**
  给节点,节点用 4.18 的 `ctr tasks kill --exec-id` 杀掉(kata 下也有效)。杀了还不结束的,10 s 后照样回 Timeout 给客户端。
- **背压**:发给客户端未取走的超过 `[websocket_proxy] stream_buffer_limit`(默认 1 MiB)就停读运行时。超时由独立定时线程执行
  (主循环可能卡在向不读的客户端发送),它只负责结束命令,结束状态由主循环发出。
- **不变**:同步 exec、交互式(tty)exec、1.58 的字段;不带 stream 的请求与以前完全一样。RPC 新增 `container_exec_stream`
  (call,短)与 `container_exec_kill`(cast,来自 wsproxy)。
- **部署**:`exec_token_key` 进 zun-values-secret;生产 wsproxy 在控制节点,能拨各节点 dockerd(mTLS),但**拨不到 containerd
  只听 loopback 的流服务器**——生产上胶囊的流式(以及现有交互式)exec 要等 wsproxy 上计算节点(DaemonSet,kubezun 会话跟进)。
- **验证**:单测 `zun/tests/unit/{api/controllers/v1,compute,websocket}/test_exec_stream.py`。端到端用真 wsproxy:本机 dockerd
  (stdout/stderr、退出码、stdin 关闭、二进制、32 MiB 慢读者下处理进程内存不涨、超时 3.1 s 杀掉、断开即杀、令牌拒绝/复用/过期、
  32 并发 sleep 2 共 2.7 s)、本机上游 containerd 2.2.2(v4)与 fork main.20261027(v5:`wc -c` 收到 `[255,0]` 后输出 70000)。

**2026-10-27 补修(同一批上线)**:
- **v4 客户端握手 400**:只报 `v4.channel.k8s.io` 的客户端(qinling)被 websockify 回 "Invalid protocol selected"。现在从客户端
  报的子协议里选**双方都会的最新版**(v5 运行时也能服务 v4 客户端,v4 下关 stdin 照旧回 BadRequest)。
- **结束时没有 close 帧、排队帧可能丢**:结束前先把 websockify 队列里的帧发完(`_drain`,最多 30 s),再发 `CClose(1000)`。
- **docker-py 吞掉随 101 一起到达的输出**:`exec_start(socket=True)` 把和响应头同一批读进 HTTP 缓冲的字节丢了(假服务器实测
  5008 字节一个不剩)。新 `docker_utils.start_exec()` 把缓冲里已读到的字节取出来先处理;**同步 exec 也受益**(生产现有问题:
  输出偶发缺开头)。
- **CRI 客户端断开时改硬断**:上游 containerd 对优雅关闭不杀命令,只有硬断才杀。
- **docker 连接读失败不再当成 EOF**:回 Failure "the output is incomplete",并让节点杀命令。
- **⚠ 已知未结论:docker 路径偶发丢尾**。10-27 上午 32 MiB 输出约 50 轮里 7–8 次少最后 128/160 KiB(32 KiB 整数倍),状态仍是
  Success;wsproxy 计数显示是 dockerd 给的就少(读到多少转多少、无读错误)。可疑点 moby `daemon/monitor.go:226` exec 退出后只等
  2 s 就关流,但四种复现手段都没复现,之后 110 多轮(含直读 dockerd 对照 30 轮)全完整。现在每次 docker 流结束都在 INFO 记
  "N bytes from the docker daemon",再出现时与命令实际输出量对比即可定位。CRI 路径无此现象。

### 4.21 会话令牌签名、短时、一次性;wsproxy 前置 Envoy(2026-10-27)

生产 wsproxy 改 DaemonSet(hostNetwork,每节点一份,胶囊路径才连得到 loopback 上的 containerd 流服务器)后,节点 IP 上的端口
**租户网络可直达**(10-27 实测 daas-test 租户网络能连所有基础设施网段)。原来的问题:websockify **每个连接先 fork 再验令牌**;
attach / 交互式 exec / logs 的令牌是库里的 UUID,**永不过期、可反复用**。用户定的原则:gateway 仍是默认入口,但 **wsproxy 自身
必须安全,不能只靠防火墙**。做法(用户批准 Envoy 边车方案,不自己重写服务端模型):

- **令牌改 JWT**(`zun/websocket/console_token.py`,ES256):zun-api 用 `[websocket_proxy] token_signing_key`(EC P-256 私钥 PEM)
  签,声明 `iss=zun`、`aud=zun-wsproxy`、`sub`=容器 uuid、`knd`=attach/exec/logs/stream、`eid`=exec id、`tok`=原来的令牌、`exp`。
  有效期:attach/exec/logs `console_token_ttl`(默认 60 s,只管"多久内必须连上",连上后跟会话走),流式 `exec_token_ttl`(30 s,
  里面的 `tok` 仍是 Fernet 加密的运行时地址,不给调用方看)。公钥在谁手里谁就能验:Envoy、wsproxy。
- **wsproxy**(`token_verify_key` 公钥 PEM):**所有会话都在 101 之前验**——签名、过期、`sub`/`knd`/`eid` 对得上;库里的会话
  再比 `tok` 与行里的令牌,**原子清掉**(`db.api.consume_console_token`,`UPDATE … WHERE token=?`,并发两个连接只有一个成功)。
  坏的、过期的、用过的、别的会话的 → **403 + 一句原因**,没有 101。没配公钥 = 老行为(裸 UUID、不过期),上游/devstack 不受影响。
  ⚠ 没有 Envoy 时 fork-先于-验证 仍在:**暴露给不可信网络必须前置反向代理**。
- **重连**:令牌一次性 → 重连要重新调 API(attach / execute interactive / logs)拿新地址。Horizon 刷新页面本来就重新 attach。
  响应格式不变(令牌对客户端是不透明字符串),不加 microversion。
- **Envoy 边车**(`tools/wsproxy-edge/envoy_config.py` 生成;JWKS 由 `python -m zun.websocket.console_token verify.pub` 导出):
  TLS 终结(握手超时)、`jwt_authn`(从 `token` 参数取,验签名+exp,失败 401,**wsproxy 根本看不到**)、只放 websocket upgrade
  (其余 404)、`request_headers_timeout`、按客户端地址限速(local_ratelimit 动态描述符)、全局连接上限(overload manager)、
  到 wsproxy 的会话数上限(circuit breaker)、`stream_idle_timeout`/路由超时 0(不说话的命令不是空闲)。**两个连接池**:来源在
  `TRUSTED`(gateway/节点地址)的走大池,其它来源(直连的租户)共用小池且头部超时更短——**直连洪水只能耗尽小池,碰不到 gateway
  那条路**。Envoy **不能按单个来源限并发**,这一层留给主机防火墙(nftables `ct count`)。
- **验证**(10-27,本机实验:Envoy 1.37.11 宿主网络 + 真 forking wsproxy + sqlite + 本机 dockerd/fork containerd):
  端到端 16 项全过——docker/CRI 流式经 Envoy(wss)、attach/logs、同一 URL 第二次 403、过期/伪造/无令牌/裸 UUID 在 Envoy 401 且
  wsproxy 日志 0 行、绕过 Envoy 直连 wsproxy:好令牌 101/复用 403/裸 UUID 403/流式令牌冒充 attach 403、浏览器 Origin 经 wss、
  不说话的命令(20 s 与 400 s)最后照样拿到输出和 Success。洪水:127.0.0.2 同时 1500 慢速连接 + 200 路无令牌请求 + 500 半截 TLS
  60 s,127.0.0.1 照常跑流式 exec——单池配置下 17 次失败 7 次(全局上限被占满),**分池后 16/16 成功、p50 0.159 s(无洪水 0.161 s)**,
  wsproxy 子进程峰值 1(洪水一个都没 fork 出来)。
- **心跳保活(10-27 晚补)**:生产 gateway(Cilium operator 的 `proxy-stream-idle-timeout-seconds`,默认 300,写进所有监听器的
  HCM,不能按路由覆盖;设 0 也回落到 Envoy 自己的 300)会切断 300 s 没有帧的流——`sleep 400` 的会话在 301 s 被断(OpenStack-2026.2-Upgrade
  实测)。用户不放宽全集群。新增 `[websocket_proxy] heartbeat`(秒,默认 0 不发,上游行为不变;生产设 60):attach/logs/交互式 exec
  经 websockify 自带的 heartbeat(`zun-wsproxy` 现在把它传进去),流式 exec 的两个 relay 循环在 heartbeat 秒内没给客户端发过任何帧、
  且发送队列为空、socket 可写时发 ping。本机复现:Envoy 去掉路由级 `idle_timeout`、`stream_idle_timeout: 30s` 模拟 gateway,命令沉默
  45–90 s——heartbeat=0 四种会话(attach、logs、docker 流式、CRI 流式)都在 ~31 s 被断;heartbeat=10 全部收到最后的输出和 Success。
  ⚠ 实验时注意:Envoy 路由级 `idle_timeout` 会覆盖 HCM 的 `stream_idle_timeout`(边车配置里设 0 就是为此),拿它模拟 gateway 要去掉。

### 4.22 systemd cgroup 驱动的节点上 runc capsule 起不来(2026-10-27)

zun-worker 同时是 Kubernetes 节点,containerd 的 runc 为 kubelet 配了 `SystemdCgroup = true`。驱动给沙箱的 cgroup 父路径
一直是 cgroupfs 写法 `/zun.slice/<uuid>`,runc 直接拒绝:`runc create failed: expected cgroupsPath to be of format
"slice:prefix:name"`——**所有 runc capsule 都起不来**(OpenStack-2026.2-Upgrade 会话发现,本机 fork containerd main.20261027 +
SystemdCgroup 复现)。kata capsule 不受影响(kata 的 cgroup 由 shim 管,containerd 配置里没有 SystemdCgroup)。

光改名字不够:**systemd 会把手写进 slice 文件的上限改回 max**——实测预先写好 cpu.max/memory.max,沙箱一起来就变 `max`;
起来后再写,一次 `daemon-reload` 又变回 `max`。只有 slice 自己的属性(`systemctl set-property`)经得住。所以改成和 kubelet 的
systemd 驱动同样的做法:

- **判断**:`[DEFAULT] cri_cgroup_driver`(auto/cgroupfs/systemd,默认 auto)。auto 时按 runtime 读 containerd 配置
  (CRI `Status(verbose)`,和 snapshotter 同一份)里的 `SystemdCgroup`,每个 runtime 只问一次;读不到不记住,下次再问。
- **命名**:systemd 的 runtime 用 `zun-<uuid 的 - 换成 _>.slice`(systemd 把 `-` 当层级,kubelet 对 pod uid 也这么换),
  落在 `zun.slice` 下;containerd 看到 `.slice` 结尾自动用 `slice:cri-containerd:<id>` 格式。cgroupfs 的 runtime 不变。
- **pod 上限**(cri_pod_cgroup_runtimes 里的 runc/runsc):建沙箱**之前** `systemctl set-property --runtime <slice>
  CPUQuota= CPUQuotaPeriodSec= MemoryMax= MemorySwapMax=`(slice 不存在时 systemd 自己建),失败即拒绝(不让租户无限制跑)。
- **块 IO**:`IOWeight=`、`IO{Read,Write}{Bandwidth,IOPS}Max=/dev/block/MAJ:MIN N` 设在 slice 上。
- **删除**:`systemctl stop` + `systemctl revert`(否则 slice 和 /run/systemd/system.control 下的 drop-in 永远留着)。
- zun-compute 是特权容器、hostPID、挂了宿主 `/run`,镜像里有 `systemctl`,经宿主 systemd 的 socket 操作——**但要带
  `SYSTEMD_IGNORE_CHROOT=1`**,见下面的补记。
- **验证**:单测(命名、自动判断与缓存、配置覆盖、上限与 IO 属性、失败即拒、释放);真驱动代码对 SystemdCgroup 的 containerd 实验
  实例走完整流程:slice 名正确、cpu.max `50000 100000`、memory.max 256 MiB、swap 0、io.weight 2930、io.max wbps=10 MiB/s,
  `daemon-reload` 后不变,沙箱 scope 在 slice 下,删除后 slice 与 drop-in 都没了。

**补记(同日,生产验收发现):上面的属性在生产上一个都没设上,而且没报错。**zun-compute 容器和宿主共用 PID 1,但根目录不同,
`systemctl` 据此判定自己在 chroot 里,对 set-property/show/stop/revert 一律打印 `Running in chroot, ignoring command
'set-property'` 然后**退出码 0**。驱动只看退出码,以为设上了,失败即拒从没触发;删除时 stop/revert 同样被忽略,slice 留着
(OpenStack-2026.2-Upgrade 会话在 worker2 上发现:slice 在,CPUQuota/MemoryMax/MemorySwapMax 全是 infinity;各容器自己的
cpu.max/memory.max 由 runc 写在容器 scope 上,仍然生效,缺的是 pod 上限)。上面的实验是在宿主上直接跑驱动代码,不在
chroot 里,所以没暴露。修法:

- 所有 systemctl 调用带 `SYSTEMD_IGNORE_CHROOT=1`:要操作的本来就是宿主 systemd,经共享 `/run` 下它的 socket 连得到。
- **不信退出码,回读**:set-property 之后 `systemctl show` 这些属性;按原名显示的要和设的值逐字相同,改了名显示的
  (CPUQuota→CPUQuotaPerSecUSec,CPUQuotaPeriodSec→CPUQuotaPeriodUSec,单位也变)要求不再是 infinity。不符即拒绝建沙箱,
  报出哪几个属性没设上。
- **验证**:新单测(chroot 被忽略时失败即拒、回读不符时失败即拒;在无修复代码上先看它失败);用生产镜像 05fe9d4e,
  特权 + `--pid=host` + 挂宿主 `/run`(和 zun-compute 相同)跑改后的驱动代码:不带变量时复现 `Running in chroot` + rc 0,
  带变量后宿主上 MemoryMax 268435456、CPUQuotaPerSecUSec 500ms、IOWeight 100,一个 systemd 不接受的值被拒绝,释放后
  drop-in 清零。

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
