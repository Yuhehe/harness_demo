# Docker 相关知识笔记

> 本文是学习笔记，不含实验指标。
> 编排：先说**本项目需要 Docker 的哪些特性**（§1），再按**底层原理**展开（§2），最后记录 **WSL2 环境的安装与配置**（§3）。
> 与 `CLAUDE.md` 的对应规则：§2.2（沙箱）、§3.1（固定基座）、§5.5（沙箱设计）、§5.6（权限层）。

---

## 1. 本项目需要的 Docker 特性

项目用 Docker 做两件事：

| 用途 | 说明 | 规则来源 |
|---|---|---|
| **沙箱** | 模型给出的命令只能在容器里执行，宿主机禁止 `subprocess` 运行模型字符串 | CLAUDE.md §2.2、§5.5 |
| **交付** | FastAPI 服务打成镜像，`docker compose` 一键启动，CI 里跑回放冒烟测试 | CLAUDE.md §3.1、§7 |

### 1.1 沙箱用到的特性

| 特性 | 项目中的用途 | 底层机制（见 §2） |
|---|---|---|
| 官方单题镜像 | SWE-bench 每题一个镜像，代码在 `/testbed`，已切到基准提交 | 镜像分层 + OverlayFS |
| `network_mode="none"` | 默认断网，防止外联、下载、外传数据 | network namespace |
| 内存、CPU 上限 | 防止失控任务拖垮宿主机，多 worker 互不影响 | cgroup（`memory.max`、`cpu.max`） |
| `pids_limit` | 防御 fork bomb | cgroup（`pids.max`） |
| 非特权模式 | 不给容器额外能力 | capabilities、seccomp |
| 任务结束删除容器 | `try/finally` 里 `remove`，避免容器和可写层堆积 | 容器生命周期 |
| `exec` / `read_file` / `write_file` / `git_diff` | 沙箱对外接口 | Docker Engine API（经 docker.sock） |
| 容器内 `/tmp/harness/out_<n>.txt` | 可恢复截断：完整输出写在容器可写层，模型需要时再读 | OverlayFS 可写层 |

### 1.2 交付用到的特性

| 特性 | 项目中的用途 |
|---|---|
| Dockerfile + 多阶段复制 | 基于 `python:3.12-slim`，从官方 uv 镜像复制 `uv` 二进制，`uv sync --frozen --no-dev` 安装依赖 |
| BuildKit / buildx | `docker compose build` 构建服务镜像时使用 |
| `docker compose` v2 | 一键启动服务，只能用 v2 插件语法，禁用 v1 的 `docker-compose` |
| docker.sock 挂载 | 服务容器要创建沙箱容器，需要访问宿主机 Docker（见 §2.5） |

### 1.3 项目里最重要的几条约束

1. **沙箱容器绝对不挂 docker.sock。** 否则模型一条命令就能控制宿主机，断网、限额、权限层全部失效。
2. **沙箱容器用完必删**，用 `try/finally` 保证。
3. **镜像命名规则先查 `swebench` 或 mini-swe-agent 源码再用，不要猜**（CLAUDE.md §5.5）。
4. 固定版本时要用镜像 digest 或明确的 tag，**tag 是可变指针**，影响"冻结版本"后实验可复现。

---

## 2. 原理

### 2.1 整体架构：Docker 是客户端/服务端

`docker` 命令只是客户端，真正干活的是后台的 `dockerd`。所有客户端都经 Unix socket 与它通信。

```
 docker CLI ──┐                          ┌─ buildx 插件 ──→ BuildKit（构建引擎）
 Python SDK ──┼─→ /var/run/docker.sock ─→│
 compose 插件 ┘      (Unix socket)       └─→ dockerd ──→ containerd ──→ runc ──→ 容器进程
                                                                        │
                                              内核：namespace（看见什么）+ cgroup（用多少）+ overlayfs（文件）
```

要点：

- **容器不是小虚拟机，而是被隔离、被限额的普通 Linux 进程。** 这是理解后面所有内容的前提。
- 隔离与限额由 **Linux 内核**实现，Docker 只负责配置。

### 2.2 镜像、容器、实例

**镜像（Image）：只读模板**

- 一叠只读的文件系统层，加一份 JSON 元数据（默认命令、环境变量、工作目录等）。
- 每层是一个 tar 包，记录相对上一层增加、修改、删除了哪些文件，用**内容的 sha256 哈希**命名。内容相同的层只存一份，所以多题共享环境层，开销小。
- `name:tag` 只是指向某个镜像哈希的**可变指针**，要可复现就固定 digest（`@sha256:...`）。

**容器（Container）：镜像 + 可写层 + 被隔离的进程**

1. **文件系统**：镜像的只读层加最上面的可写层，由 OverlayFS 叠成统一视图
2. **一组内核隔离与限额**：namespace + cgroup
3. **一个入口进程**：容器的 PID 1，它退出，容器就结束

OverlayFS 的四个目录：

| 目录 | 含义 |
|---|---|
| `lowerdir` | 镜像的只读层（可有多层） |
| `upperdir` | 容器的可写层，初始为空 |
| `merged` | 叠加后的视图，也就是容器看到的 `/` |
| `workdir` | OverlayFS 内部临时目录 |

- **读**：先找 upper，没有再找 lower。
- **改**：改 lower 里的文件时，先整个复制到 upper（copy-up），再改 upper 里的副本，lower 不动。
- **删**：在 upper 里放一个遮罩标记（whiteout），lower 里的文件仍在，但看不见。

所以**模型在沙箱里做的所有修改都只在 upper 里**，删除容器后消失，镜像原封不动。这是"每题互相隔离、环境可复现"的根本原因。

**生命周期**：`created → running → exited → removed`。`exited` 的容器仍占磁盘（可写层还在），直到 `docker rm`。批量跑 50 题不删容器就会堆积。

**"实例"一词有两个含义，容易混淆**

| 语境 | 含义 |
|---|---|
| Docker 的非正式说法 | 容器是镜像的一个"实例"（类比：类和对象，程序文件和进程） |
| SWE-bench | `instance` 是一道题（`instance_id`，如 `django__django-11099`），每题有自己的 instance image |

项目里的对应关系：

```
1 道题 (instance_id) → 1 个题目镜像 → 每次运行创建 1 个容器 → 结束后删除
```

### 2.3 资源控制：namespace 和 cgroup

| 机制 | 控制什么 | 一句话 |
|---|---|---|
| **namespace（命名空间）** | 能**看见**什么 | 给进程一个私有视图 |
| **cgroup（控制组）** | 能**用多少** | 给一组进程设资源上限 |

执行链路：`docker run` → dockerd 生成 **OCI 规范（config.json）** → containerd → **runc** 读规范，执行 `clone()`（带 namespace 标志）、创建 cgroup 并写限额文件、设置 capabilities 和 seccomp、`pivot_root` 切换根目录，最后 `exec` 入口进程。这些都由内核强制执行，容器内的进程无法自行修改。

#### 网络：network namespace

- 每个 net namespace 有独立的网卡、IP、路由表、iptables 规则。
- 默认 bridge 模式：Docker 创建一对 **veth**（虚拟网线），一头在容器里，一头接在宿主机的 `docker0` 网桥上，再经 iptables NAT 出网。
- **`network_mode="none"`**：只创建 net namespace，里面只有回环接口 `lo`，**没有 veth，没有路由**。

断网**不是用防火墙过滤**，而是容器里根本没有通往外部的网卡，所以比规则过滤更难绕过。

#### 内存、CPU、进程数：cgroup v2

cgroup v2 是挂在 `/sys/fs/cgroup` 的目录树，**每个容器对应一个子目录**，控制就是往目录里的文件写数字：

| Docker 参数 | cgroup 文件 | 超限时发生什么 |
|---|---|---|
| `--memory 64m` | `memory.max = 67108864` | 先尝试回收页缓存，不够则触发 OOM killer 杀进程，容器退出码 **137**（128 + SIGKILL 的 9） |
| `--cpus 0.5` | `cpu.max = "50000 100000"` | 每 100ms 周期最多用 50ms CPU，超了被**暂停（限流）**，不杀进程，只是变慢 |
| `--pids-limit 20` | `pids.max = 20` | 第 21 个 `fork()`/`clone()` 直接失败，返回 `EAGAIN`（Resource temporarily unavailable） |

#### 其他加固机制

- **capabilities**：把 root 的权力拆成几十个小权限，容器默认只保留少数几个
- **seccomp**：过滤危险系统调用
- **`no-new-privileges`**：禁止通过 setuid 提权
- 可选进一步收紧：`cap_drop=["ALL"]`、只读根文件系统（这是加固选项，项目文档未强制要求）

#### 容易踩的坑

1. **容器里的 `free`、`nproc` 显示的是宿主机的数字**，因为 `/proc/meminfo` 没有被虚拟化。真实限额要读 `/sys/fs/cgroup/memory.max`。
2. **`--pids-limit` 是整个容器所有进程的总和**，`docker exec` 出来的进程也算。
3. **`exec_run` 没有超时机制**，客户端停止等待并不会杀掉容器里的进程。`bash` 工具要实现 120 秒超时，得自己处理（例如在容器内用 `timeout` 命令包一层），否则超时后进程仍在运行，占用资源和 pids 额度。
4. **WSL2 多一层限制**：`.wslconfig` 限制整个 WSL 虚拟机，容器的 cgroup 是它的子集。所有 worker 的容器限额之和要小于 WSL 总量。

### 2.4 buildx 与 docker compose v2

两者都是 **docker CLI 的插件**，位于 `/usr/libexec/docker/cli-plugins/` 一类目录。`docker <子命令>` 找不到内置命令时会去这里找同名插件，找不到就报 `unknown command`。**它们本身都不创建容器，只是向 daemon 或构建引擎发请求。**

**buildx：构建的前端，对接 BuildKit**

- 构建指把 Dockerfile 变成镜像。旧构建器逐条顺序执行；**BuildKit** 把 Dockerfile 解析成依赖图（DAG），无依赖的步骤并行执行，缓存按内容哈希命中。
- BuildKit 支持 `RUN --mount=type=cache`（跨构建保留 uv 缓存）、`--mount=type=secret`（构建时用密钥而不留在镜像里）、多阶段并行等。
- buildx 管理 builder（构建引擎实例，有 `docker`、`docker-container` 等 driver），支持多平台构建（`--platform`）和远程缓存导入导出。
- 现在的 `docker build` 也是转调 buildx。
- 对本项目：只影响**服务镜像**的构建（包括 `COPY --from=<uv 镜像>` 这类跨镜像引用），**和 SWE-bench 沙箱无关**，沙箱直接用官方镜像。

**docker compose v2：多容器声明式编排**

1. 读取并校验 YAML，合并环境变量
2. 按 `depends_on` 排出依赖顺序
3. 调用 Docker API 创建网络、卷、容器，并打上标签（`com.docker.compose.project=...`），`down` 时据此清理
4. 需要构建时委托给 BuildKit

`up` 是创建并启动，`down` 是停止并清理。所有操作最终都是对 dockerd 的 API 调用。

**v1 与 v2**：v1 是 Python 写的独立程序 `docker-compose`（带连字符），已停止维护；v2 是 Go 重写的 docker 插件，命令是 `docker compose`（空格）。**项目规定只用 v2**，所以 apt 里 `docker-compose 1.29.2` 不能装。

### 2.5 docker.sock

**它是什么**

Unix domain socket 是**特殊类型的文件**（`ls -l` 里权限开头的 `s`），是同一台机器上进程间通信的端点：一个进程 `listen()` 监听这个路径，另一个进程 `connect()` 这个路径就能通信，访问权限由普通文件权限（owner、group、mode）控制。

`/var/run/docker.sock` 是 **dockerd 监听的 API 入口**，里面跑 HTTP 协议（REST API）：

```bash
curl --unix-socket /var/run/docker.sock http://localhost/version
```

`docker` 命令、Python Docker SDK、compose、buildx 都是这个 API 的客户端。`DOCKER_HOST` 环境变量可以把入口改成 tcp 或 ssh。

**与容器的区别**

| | docker.sock | 容器 |
|---|---|---|
| 本质 | 文件系统里的通信端点 | 被隔离、被限额的进程 |
| 角色 | 管理者的大门（控制面） | 被管理的对象 |
| 谁创建 | dockerd 启动时 | 通过 API 请求创建 |

socket 不是容器，它是控制所有容器的**权力凭证**。

**挂载 docker.sock**

```bash
docker run -v /var/run/docker.sock:/var/run/docker.sock ...
```

这是一个 **bind mount**：在容器的 mount namespace 里，把宿主机的这个文件节点映射到容器路径。容器里的进程于是能 `connect()` 它，连上的就是宿主机上的 dockerd。

- **`--network none` 拦不住它**：Unix socket 按文件路径寻址，不走网络栈，而 network namespace 只隔离网络。
- **等价于宿主机 root**：容器内进程可调用任意 Docker API，例如创建一个特权容器并把宿主机 `/` 挂进去。
- 同理，**用户加入 `docker` 组也等价于 root**（见 §3.4）。

**容器里用 Docker 的两种做法**

| 做法 | 原理 | 代价 |
|---|---|---|
| DooD（Docker-out-of-Docker）：挂 sock | 新容器是宿主机上的**兄弟容器** | 简单，但服务容器拥有宿主机控制权 |
| DinD（Docker-in-Docker） | 容器里再跑一个 dockerd | 需要 `--privileged`，更重，隔离问题更多 |

**路径坑**：通过 sock 创建兄弟容器时，`-v 宿主路径:容器路径` 里的宿主路径指的是**真正宿主机的路径**，不是服务容器里的路径。

**对本项目**

```
宿主机 dockerd
   ├── 服务容器（FastAPI + worker）  ← 运行 harness 自己的代码，受信任，可以挂 docker.sock
   └── 沙箱容器 × N（SWE-bench 镜像） ← 运行模型的命令，不受信任，必须：
                                          ✗ 不挂 docker.sock
                                          ✓ network none、资源限额、非特权、用完即删
```

服务容器挂 sock 的风险边界在于：里面只跑自己写的代码，模型的字符串永远不在这里执行。这正是 CLAUDE.md 规则 2 的用意。

### 2.6 隔离的边界

容器不是虚拟机，所有容器**共享宿主机内核**（这里是 WSL2 的 Linux 内核）。隔离靠内核机制，强度弱于虚拟机。因此项目采用多层防御，没有哪一层被当作绝对安全：

```
断网 + 资源限制 + 非特权 + 权限层 guard + 用完即删 + 宿主机不执行模型字符串
```

### 2.7 Docker Engine 与 Docker SDK for Python：为什么装了 Docker 还要 `uv add docker`

这是**两个不同的东西，只是名字相同**：

| | apt 装的 `docker.io` | `uv add docker` |
|---|---|---|
| 是什么 | Docker Engine：后台的 `dockerd` 守护进程，加上 `docker` 命令行 | PyPI 上的 Python 库（docker-py，即 Docker SDK for Python） |
| 装在哪 | 系统里 | 项目的 `.venv` 里 |
| 作用 | 真正创建和运行容器 | 让 Python 代码向 `dockerd` 发请求 |
| 能否互相替代 | 不能 | 不能 |

`uv add docker` 只装一个 Python 客户端，**不会安装 Docker 本身**。它和 `docker` 命令行同属一层，都是 §2.5 里 docker.sock 那个 REST API 的客户端：

```
docker 命令行 ──┐
Python SDK   ──┼─→ /var/run/docker.sock ─→ dockerd（apt 装的）→ 容器
compose 插件 ──┘
```

两者缺一不可：没有 Engine，SDK 无处可连；没有 SDK，harness 的 Python 代码没法方便地操作容器。

**为什么项目用 SDK，而不是在 Python 里 `subprocess` 调 `docker` 命令行**

1. **规定如此**：CLAUDE.md §3.1 要求"沙箱通过 Docker SDK for Python 创建容器"。
2. **安全**：用 `subprocess` 拼 `docker exec ... <模型给的命令>`，容易出现字符串拼接和转义问题，模型输出里的引号、分号可能变成宿主机上的 shell 注入。SDK 把命令作为参数列表传给 API，不经过宿主机的 shell。这也呼应规则 2：宿主机上禁止用 `subprocess` 运行模型产生的任何字符串。
3. **工程上更可靠**：返回结构化对象（容器状态、退出码、日志流），出错抛异常，不用解析命令行文本；资源限制直接传参数（`network_mode="none"`、`mem_limit`、`pids_limit`）。
4. **便于测试**：外部依赖可以换成假对象，符合 CLAUDE.md §8"Docker、LLM 都用假对象或回放代替"。

**服务镜像里同理**：`docker/Dockerfile` 里 `uv sync` 会装 SDK，但镜像里**不需要装 Docker Engine**，服务容器通过挂载宿主机的 `docker.sock` 去连宿主机的 `dockerd`（见 §2.5）。

---

## 3. WSL2 环境安装与配置

### 3.1 选型结论

**在 WSL2 的 Ubuntu 里直接安装原生 Docker Engine，开启 systemd，不用 Docker Desktop。**

| 项目要求 | 原生 Engine | Docker Desktop |
|---|---|---|
| Docker SDK for Python 连 `/var/run/docker.sock` | 直接可用 | 靠 WSL 集成代理，多一层 |
| `docker compose` v2 | 有 | 有 |
| `network_mode="none"`、`pids_limit`、非特权、内存/CPU 限制 | 内核原生特性 | 同样支持 |
| 沙箱安全 | Docker 只在 WSL 里，不暴露给 Windows | 默认暴露到 Windows，攻击面更大 |
| 内存、CPU 可控 | 直接吃 WSL 配额 | 多一个 VM 层，占用更高 |
| 许可 | 免费 | 大公司使用需付费 |

仓库放在 WSL 的 ext4（`/home/...`），**不要放 `/mnt/c`**，否则文件 IO 很慢。

### 3.2 开启 systemd

**为什么**：systemd 不是 Docker 必需的，但 Docker 的 apt 包按 systemd 系统设计，装完后由它管理 `dockerd`，才能 `systemctl enable --now docker` 并自启。不开的话每次打开 WSL 都要手动 `sudo service docker start`，daemon 没起时报 `Cannot connect to the Docker daemon` 也容易误判成配置错误。

```bash
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```

然后在 **Windows PowerShell** 执行 `wsl --shutdown`，重新打开 WSL。**重启前先保存工作**，所有 WSL 终端和进程都会被关掉。

影响：启动多几秒；多几个后台服务（如 `snapd`，不需要可禁用）；PID 1 变成 systemd；有服务在跑时实例不会立刻因无终端而停止。**可回滚**：删掉 `systemd=true` 再 `wsl --shutdown`。

### 3.3 apt 源：用 Ubuntu 自带源即可

最初推荐官方源 `download.docker.com`（版本最新、插件齐全），但实际检查发现：

- 本机 apt 已配置**清华镜像**，访问 `download.docker.com` 在国内常常很慢或不稳定
- Ubuntu 自带源里的版本并不落后

| 自带源的包 | 候选版本 | 说明 |
|---|---|---|
| `docker.io` | 29.1.3 | 引擎 |
| `docker-compose-v2` | 2.40.3 | 提供 `docker compose`，符合 v2 要求 |
| `docker-buildx` | 0.30.1 | buildx 插件 |
| `docker-compose` | 1.29.2 | **v1，项目禁用，不要装** |

**结论：直接用自带源。** 代价：版本跟随 Ubuntu；`docker.io` 与官方的 `docker-ce` 互相冲突，不能混装。若以后版本不够，可改用清华的 docker-ce 镜像（`https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu`），仍无需访问国外域名。

### 3.4 安装、自启、用户组

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 docker-buildx
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

**`systemctl enable --now docker`**

| 部分 | 含义 |
|---|---|
| `enable` | 创建启动链接，下次启动时自动运行，**不立刻启动** |
| `--now` | 同时立刻启动，等价于再加一条 `start` |
| `docker` | 服务单元 `docker.service` |

"开机"在 WSL2 里指 **WSL 实例启动时**，不是 Windows 开机。常用配套命令：

```bash
systemctl status docker          # 状态
sudo systemctl restart docker    # 改 daemon.json 后重启
journalctl -u docker -e          # daemon 日志
```

**`usermod -aG docker $USER`**

`/var/run/docker.sock` 属于 `root:docker`，权限 `660`，只有 root 和 `docker` 组成员能读写。不加组则每个 docker 命令都要 `sudo`，而 `sudo` 会让 `uv` 找不到虚拟环境、产出文件所有权变成 root，Docker SDK 也会报 `PermissionError`。

- `-G docker`：设置附加组；`-a`：追加。**必须带 `-a`**，否则会覆盖原有附加组（如 `sudo`），可能丢掉 sudo 权限。
- 组关系只在**登录会话开始时**读取。生效方式：`newgrp docker`（只对当前 shell），或 `wsl --shutdown` 后重开终端。
- **安全提醒**：`docker` 组成员等价于宿主机 root（见 §2.5）。单人开发的 WSL 环境可接受。rootless Docker 配置更复杂，且与项目设计假设不一致，本项目不采用。

### 3.5 本机已验证的环境信息

| 项 | 值 |
|---|---|
| 系统 | Ubuntu 22.04，WSL2，内核 6.18.40.1-microsoft-standard-WSL2 |
| 资源 | 12 核，约 15GB 内存 |
| cgroup | v2，驱动 systemd |
| 存储驱动 | overlayfs |
| 运行时 | runc |
| socket | `srw-rw---- root docker /var/run/docker.sock` |

### 3.6 `.wslconfig` 资源限制

在 Windows 的 `%UserProfile%\.wslconfig` 里设置，防止多 worker 并行时拖垮宿主机：

```ini
[wsl2]
memory=12GB
processors=10
swap=8GB
```

### 3.7 其他注意事项

1. **不要与 Docker Desktop 并存**。若 Windows 上已装，在 Settings → Resources → WSL integration 里关闭对本发行版的集成，否则 `docker` 命令和 context 会冲突。
2. **磁盘**：SWE-bench 50 题的单题镜像加环境镜像占空间大，预留几十 GB。WSL 的 VHDX 默认在 C 盘。
3. **镜像拉取**：无论 apt 用什么源，拉 SWE-bench 镜像都要访问 Docker Hub，国内网络可能需要在 `/etc/docker/daemon.json` 配 registry mirror。
4. **镜像架构**：官方 SWE-bench 镜像是 x86_64，与本机架构匹配。

### 3.8 排错记录

| 现象 | 原因 | 解决 |
|---|---|---|
| `docker: unknown command: docker buildx` | `docker.io` 包不带 buildx 插件，它是单独的包 | `sudo apt-get install -y docker-buildx` |
| `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock` | daemon 在运行，但当前 shell 的用户组未刷新 | `newgrp docker`，或 `wsl --shutdown` 后重开 |
| `Cannot connect to the Docker daemon` | daemon 未启动（通常是 systemd 没开或没 `enable --now`） | 检查 `/etc/wsl.conf`，`systemctl status docker` |

---

## 4. 验证命令

需要先 `docker pull alpine`（会访问 Docker Hub）。这些行为后续可直接写成沙箱的单元测试或集成测试，用来证明限制确实生效。

```bash
# 基础验证
docker version && docker compose version && docker buildx version
docker run --rm hello-world

# 断网：访问外部应失败
docker run --rm --network none alpine wget -T 3 -qO- http://example.com

# 容器内直接读 cgroup 文件，应看到 67108864、50000 100000、20，网卡只有 lo
docker run --rm --network none --memory 64m --cpus 0.5 --pids-limit 20 alpine \
  sh -c 'cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max /sys/fs/cgroup/pids.max; ls /sys/class/net'

# 进程数限制：超过 20 个会 fork 失败
docker run --rm --pids-limit 20 alpine sh -c 'for i in $(seq 1 50); do sleep 5 & done; wait' 2>&1 | head -3

# 内存限制：超限被杀，退出码 137
docker run --rm --memory 32m alpine sh -c 'head -c 100m /dev/zero | tail'; echo "exit=$?"

# 看 docker.sock 就是 HTTP API
curl --unix-socket /var/run/docker.sock http://localhost/version
```

---

## 5. 待办与未决问题

- [ ] 服务容器的 compose 设计：是否挂 docker.sock、以哪个用户运行、`group_add` 的 docker 组 GID，等做交付那天再定
- [ ] `bash` 工具的 120 秒超时在容器内如何实现（见 §2.3 坑 3），需要保证超时后清理残留进程
- [ ] SWE-bench 官方镜像命名规则，查 `swebench` 或 mini-swe-agent 源码后记入 `docs/notes.md`
- [ ] Docker Hub 拉取速度，必要时配 registry mirror
- [ ] 沙箱是否进一步使用 `cap_drop=["ALL"]`、`no-new-privileges`，在安全评测阶段决定
