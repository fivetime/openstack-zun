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

**按 `RuntimeService` 的 37 个 RPC 划线,不按"薄不薄"猜。**之前把
pause/unpause 列进"第二档,都薄"是错的——CRI **根本没有 pause 这个调用**。

| ContainerDriver 方法 | 状态 |
|---|---|
| create/delete/start/stop/show/list/`get_websocket_url`/镜像 | **已实现并实测** |
| reboot / kill / update / stats / top | **已实现并实测** |
| **pause / unpause** | **做不到**:CRI 无对应 RPC |
| **resize**(tty 尺寸) | **做不到**:CRI 无 resize;尺寸走流内第五通道 |
| **network_attach / network_detach** | **做不到**:沙箱的网络在创建时定死 |
| commit / get_archive / put_archive | 做不到,且无需求 |

⚠️ 几处**语义不等价**,不是实现细节:

- **`kill` 不是 docker 的 kill**。CRI 只能"停",不能"发信号"。SIGKILL/SIGTERM
  恰好可以表达成"停"(有无宽限期),**其他信号不行**——用 SIGHUP 让服务重载配置
  在这里没有对应物,而"那就把容器停了"是用杀掉工作负载来回答一个重载请求。
  所以其他信号明确报错,不静默降级。
- **stop/start 与 reboot 会丢掉可写层**。CRI 不能重启已退出的容器,只能在原沙箱里
  重建;**地址保住了**(地址属于沙箱),**文件没保住**(docker 的 restart 保得住)。
  marker 文件实测:reboot 后 `/tmp/before-reboot` 不见了。
- **`stats` 的 CPU 必须采两次**。运行时给的是累计纳秒计数器,单次采样除以任何东西
  都得不到百分比——只会得到一个长得像百分比的数。BLOCK/NET I/O 运行时不记,报
  `-/-` 而不是 0(0 读起来是"空闲",不是"没人量过")。

### 4.3.2 两次"服务两种形态"打断了 capsule

都在共享代码上,都只伤新建、不伤在跑的,所以**极易漏过**:

1. **周期状态同步遍历 `unit.containers`**,原生容器没有这个属性 → 抛异常。
   而**一次抛出结束整趟清扫**,于是那台节点上**所有 capsule 也不再被对账**。
   假定 capsule 的方法遇到 container 不是"跳过",是"把整件事拖下水"。
2. **预拉取用 `hasattr(driver, 'pull_image')` 当能力探针**。为 images API 加上
   这个方法,探针就自己翻转了:每个新 capsule 开始预拉它自己的沙箱镜像
   `kubernetes/pause`——那不是一个真镜像。**调用方依赖的能力必须被声明,不能从类
   的形状推断**(现为 `pulls_own_images`,且按对象自己的驱动读)。

### 4.3.3 验收方法:202 不是证据

⚠️ **没实现的动作照样回 202**——API 只负责受理,`NotImplementedError` 发生在
计算节点上,调用方看不到。首轮 22 项里有**三项通过却什么都没做**:`reboot` 的
终态和起始态都是 Running、`kill` 之后再没人看过那个容器、`network_detach` 之后
没人看过地址。

**当"成功"和"静默无操作"产生同一个状态时,判据必须去找动作发生过的证据。**
(同一个陷阱咬了两次:第二轮我用 `started_at` 判 reboot,而那个字段根本不在租户
视图里,于是 `None -> None` 又给了一个错误结论。最后用 marker 文件才判准。)

### 4.3.4 契约测试

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

## 五、计量与配额

- **配额**:上游现成(`common/quota.py` + `quotas`/`quota_usages` 表 +
  `/v1/quotas`),按 project 记,capsule 与 container 天然同账。
- **计量**:⚠️ **Zun 不发任何通知**——唯一的 notifier 引用在
  `common/exception.py:56-84`,且无调用点传值,是死代码。但**走 ceilometer 不需要
  补通知**:用 central pollster 路径(参照 `ceilometer/load_balancer/octavia.py`
  128 行 + `ceilometer/volume/discovery.py` 58 行),查服务 API 列资源、产出样本,
  全部工作在 ceilometer 侧,Zun 不用动。数据源就是 §3.3 的 capsule stats,而且
  CPU 本来就是累计值,正好对上 ceilometer 的 cumulative 样本类型。

## 六、验证

改了 capsule schema / driver 加载 / CRI 版本,需要证明 API 契约没破:
`zun-tempest-plugin` 的 `test_capsules.py` 是最贴近我们用法的一组。
