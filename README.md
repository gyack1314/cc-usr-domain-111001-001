# 消防科普公告的可核对增量采集资料源

本项目保存本地合成资料、对应的来源站行为，以及一个轮次式公告采集模块 `notice_harvest/`。内容取自虚构场景，不代表真实活动、人员、设施或设备参数。来源服务只模拟公开读取与指定的表单行为。

## 环境与运行

使用 Python 3.11 及以上版本，仅依赖标准库。来源只监听本机地址，端口可以通过参数调整。两个终端分别启动不同端口可以表示两个独立来源。

```sh
python -m source_site --port 8090
```

`source_site/source.py` 管理来源响应，`source_site/__main__.py` 是 HTTP 适配层，`sources/` 保存原文与接口约定。状态只存在来源进程内，重启来源会恢复初始样本。`/__control` 和 `/__calls` 属于本地运维入口，资料接入端的正常操作不使用它们。运维端可以通过 POST `/__control` 提供 JSON 对象改变 phase 或更新 state。phase 列表与业务接口见下文，细节见 `sources/contract.json`。

- `GET /list?page=N`：每页两项，rel=next 翻页，来源编号可跨页重复
- `GET /detail/{id}`：详情HTML与ETag，支持If-None-Match
- `phases`：["base", "revised", "missing", "partial", "moving", "loop"]

用 Python 的 HTTP 客户端推进来源状态，例如：

```sh
python -c 'import urllib.request; r=urllib.request.Request("http://127.0.0.1:8090/__control", data=b"{\"phase\":\"base\"}", headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(r).read().decode())'
```

## 公告采集模块 notice_harvest

`notice_harvest/` 是资料接入端，只通过 HTTP 读取来源站，把可核对的证据与状态写入本地状态目录，并输出每轮的变化。模块入口可配置来源地址、状态目录和轮次标识：

```sh
python -m notice_harvest scan \
  --site http://127.0.0.1:8090 \
  --state-dir ./state \
  --round R1
```

输出为 JSON，主要字段：

- `complete`：本轮是否完整；不完整时 `issues` 给出原因（`page_fetch_failed`、`duplicate_cursor`、`list_changed`、`list_unstable`、`detail_304_without_body` 等）。
- `changes.new` / `changes.revised` / `changes.withdrawn` / `changes.unconfirmed`：下游需要的四类变化。
- `absent_pending`：完整扫描中缺席但计数未满 2 的来源编号；`restored_ids`：撤下后重新出现并恢复的编号。
- `evidence`：清单、列表页、详情、状态与轮次文件的持久化地址（相对状态目录），正文均带 `body_sha256`。

### 身份与内容

- **来源编号是同站身份**。列表中跨页重复的置顶项只产生一篇记录；URL 改写（如 `/detail/n1?print=1`）不会产生第二篇，只更新地址。
- 内容指纹覆盖标题、活动时间与正文，标题不变也不会遮住正文变化；仅 URL 变化时记入 `url_changes`，不算正文修订。
- 每篇详情按来源编号单独缓存 ETag，ETag 与原始正文写在同一个原子 JSON 中：200 时成对更新，304 时校验响应 ETag、本地 ETag 与正文哈希三者一致后才复用，绝不把旧正文与新 ETag 混用。

### 轮次完整性与补扫

列表按更新时间倒序、每页两项，翻页期间可能发布新公告。每轮至少读取两遍列表：

1. 第一遍跟随 `rel=next` 读取全部页，并记录游标；再次遇到已访问游标即判定 `duplicate_cursor` 并停止。
2. 第二遍作为确认/补扫，比较两遍的页面签名（来源编号、地址、标题与 next 链接）。
3. 签名不一致则继续补扫，直到连续两遍一致；即使最终稳定，只要观察到「扫描中变化」，本轮仍标记为不完整（`list_changed`），补扫合并到的新公告照常取证。
4. 取页失败、重复游标、列表未稳定，或详情无法确认（取页异常、304 但本地没有对应正文、解析失败、详情身份不一致）都会保留已取得证据并把本轮标记为不完整。

不完整轮次**不推进**任何来源编号的缺席计数，也不会撤下来源；列表中确实观察到的编号会清掉缺席计数。只有**连续两次完整扫描都缺席**的编号才标记撤下；撤下后重新出现即恢复（在 `new` 中以 `restored: true` 给出）。

### 幂等与崩溃恢复

- 同一个轮次标识重复执行，直接返回已提交的相同结果，不再发起 HTTP 请求。
- 所有文件通过临时文件 + 原子替换写入。详情证据先落盘、轮次最后才提交：若进程在详情已保存但轮次未提交时退出，用同一轮次标识重跑即可恢复，暂存的 ETag 会通过 304 校验后复用。
- 提交点是轮次文件 `rounds/<轮次>.json`（内含结果与 `state_after`）；即使随后的 `state.json` 快照没写完，下次运行也会从轮次文件重建状态。

### 状态目录布局

```text
<state-dir>/sites/<site-key>/
├── state.json                 # 最新已提交状态快照（可由轮次文件重建）
├── .lock                      # 同站串行执行的文件锁
├── details/<来源编号>.json     # ETag 与原始正文成对的详情缓存
└── rounds/
    ├── <轮次>/                 # 轮次暂存：manifest、pages/、details/ 证据
    └── <轮次>.json             # 已提交轮次（结果 + state_after，单文件提交点）
```

多个来源可以共用一个状态目录：每个来源按 `site-key` 隔离，同一轮次标识在不同来源下互不影响。

### 端到端演示

`scripts/demo.py` 会在临时端口启动本地来源，从干净目录依次演示首次扫描、修订、半途失败、两次完整缺席后撤下、重新出现恢复，以及翻页中插入与重复游标：

```sh
python scripts/demo.py --state-dir ./demo-state
```

## 开发检查

以下检查覆盖来源协议、异常响应、真实本地 HTTP 启动，以及采集器的端到端行为（含持久化证据地址、ETag、幂等与崩溃恢复）。检查不会要求访问公网；`tests/test_harvest.py` 中的每个用例都经过真实 HTTP 请求，而不是只调用解析函数。

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
```

容器环境也可以复现相同检查：

```sh
docker build -t notice-harvest-source .
docker run --rm --network none notice-harvest-source
```

来源的HTTP异常是合成协议行为，便于资料接入端开发和诊断。样本可扩展；业务数据、原始HTML以及接口响应不应被当作真实部署凭据或真实业务事实。
