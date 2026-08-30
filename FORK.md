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
  这一点:guest 经 virtiofs 写的就是宿主机这个目录。upperdir 的位置问 snapshot 服务
  (`snapshots.proto`,与 tasks.proto 同一种最小声明、同一个 socket、同一个
  `containerd-namespace: k8s.io` 头;snapshotter 名来自 `[container_driver]
  cri_snapshotter`,必须与节点 containerd 配置一致)。**尽力而为**:移植失败退回
  原来的"从镜像重建",`REBUILT_REASON` 只在真丢了的时候出现。
- **`stats` 的 CPU 必须采两次**。运行时给的是累计纳秒计数器。BLOCK/NET I/O 运行时
  不记,报 `-/-` 而不是 0——0 读起来是"空闲",不是"没人量过"。

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
