# 协作指南

这份文档写给一起改这个仓库的人。新人第一天照着做一遍就能跑起来。

## 一、第一次上手

```bash
git clone https://github.com/zhushuoking/feishu_agent.git
cd feishu_agent
```

Windows 上双击 `setup.bat`，它会建 venv、装依赖、生成 `.env`。

然后**填你自己的 `.env`**：

```bat
set_cred.bat     :: 填飞书 App ID / App Secret，写完会当场验证
set_model.bat    :: 选模型服务，写完会发一次真实请求验证
```

最后 `run.bat` 起长连接。

> 需要 Python 3.11 或 3.12。3.13 上部分依赖还不兼容。

## 二、最容易踩的坑：同一时间只能有一台机器跑长连接

**飞书长连接是「每个客户端都收一份事件」。** 两个人共用同一个 App ID 同时跑 `main.py`，
群里发一条消息，两个进程都会处理一遍，各自落库、各自推卡片——**群里会出现两张重复的确认卡**。

而且各自的 SQLite 是本地文件，`*.db` 不提交，所以两边的数据根本不同步，
点卡片的时候一边成功、一边报「候选不存在」。

我们**共用一套应用凭证**（`.env` 直接互相给，不走各自建应用那套），所以只有一条硬规则：

> **同一时间，只有一台机器开 `run.bat` 和 `worker.bat`。** 换人联调之前，先把自己这边的窗口关掉。

| 场景 | 怎么做 |
|---|---|
| 我自己开发 | 开 `run.bat` + `worker.bat`，改代码重启 |
| 要换他联调 | 先说一句「我关了」，**关掉两个窗口**，再让他开 |
| 只想改代码不联调 | 不用开窗口，`pytest -q` 全绿就行 |

代码合并前跑一遍单测：`pytest -q` 必须全绿（当前 415 passed）。
单测不碰飞书、不发消息，两个人可以同时跑，互不影响。

<details>
<summary>如果将来需要各自建应用（现在不用）</summary>

四个步骤少一步都不通：开启机器人能力、导入 `feishu-permissions.json`、
事件订阅 `im.message.receive_v1`（长连接）、**回调配置** `card.action.trigger`
（长连接，这是独立页面），然后创建版本并发布。

注意：外部群只支持 `im:message.group_at_msg:readonly`，`im:message.group_msg` 在外部群无效；
跨企业使用还要开「对外共享」，需要企业认证或个人实名。

</details>

## 三、分支怎么走

`main` 保持随时能跑。改动走分支：

```bash
git switch -c feat/deadline-reminder   # 新功能
git switch -c fix/card-empty-buttons   # 修 bug
```

分支名前缀用 `feat/` `fix/` `docs/` `chore/`，后面跟简短英文或拼音，能看懂就行。

改完：

```bash
git add -A
git commit -m "feat: 日报支持按项目分组"
git push -u origin feat/deadline-reminder
```

然后在 GitHub 上开 Pull Request，等一个人看过再合进 `main`。

小改动（改文档、修错别字）直接推 `main` 也行，别把自己绕进去。

## 四、提交信息怎么写

```
<类型>: <一句话说清干了什么>

<可选：为什么要改，踩了什么坑>
```

类型只有这几个：`feat` `fix` `docs` `refactor` `test` `chore`。

写「为什么」比写「改了什么」有用——半年后看 `git log` 的是你自己。

## 五、提交前必须做的三件事

**1. 测试要全绿**

```bat
.venv\Scripts\python.exe -m pytest -q
```

看到 `187 passed` 才算过。末尾如果冒出 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`，
那是环境的删除保护钩子在清理 pytest 临时目录，不影响结果，看 `N passed` 就行。

**2. 确认 `.env` 没被带上去**

```bash
git status --short          # .env 不应该出现
git diff --cached --name-only | findstr /i ".env"
```

`.env` 里有真实的 App Secret 和模型 key。**一旦推上公开仓库就等于公开了**，
GitHub 上的历史删不干净，只能去平台重置密钥。宁可多花十秒检查。

**3. 确认没有个人路径和真实 open_id**

```bash
git grep -n -i "48639\|C:\\\\Users\|anaconda"
git grep -no "ou_[a-f0-9]\{20,\}"
```

写死的绝对路径别人 clone 下来跑不通；真实 open_id 是别人的账号标识，不该进公开仓库。

## 六、编码这件事，别随手改

`*.bat` 全部是 **GBK 编码 + CRLF 换行**。这不是随便选的：

- UTF-8 的 `.bat` 里，含中文的 `echo` 行会被 cmd 错误切分，报「'按' 不是内部或外部命令」
- LF 换行的 `.bat` 双击直接闪退，没有任何提示

VS Code 右下角能看到当前编码，改成 UTF-8 后记得改回来。
`.gitattributes` 只保证换行符，**管不了编码**。

Python 文件一律 UTF-8 + LF，正常写就行。

## 七、代码上的几条约定

| 约定 | 为什么 |
|---|---|
| 模型输出必须过 `schemas.py` 的 Pydantic 校验 | 模型的输出不可信，不能直接写库、发消息、改状态 |
| 状态变化只能走 `repo.transition_task()` | 绕过它就绕过了状态机、权限和乐观锁 |
| 时间基准用消息的 `sent_at`，不用 `datetime.now()` | 补跑历史消息时用当前时间会全错 |
| 发通知只入队，不直接发 | 卡片回调只有 3 秒，网络一慢就被判超时重推 |
| 新增通知类型只写「算」那一段 | `notify.py` / `events.py` 共用同一个发件箱，别另起一套 |
| 新功能配测试 | 现有 187 条就是为了让你敢改，不加测试等于借了不还 |

## 八、目录速查

```
main.py            长连接入口，消息进来先过这里
models.py          飞书事件 → MessageCommand
store.py           event_id 去重
feedback.py        「做完了」这类回复 → 状态动作
detector.py        任务识别编排
prompts.py / llm.py / schemas.py    模型三件套
deadline.py        时间解析
db.py / models_db.py / repo.py     数据层
cards.py / feishu_api.py / card_action.py   卡片与回调
state_machine.py / permissions.py  状态与权限
notify.py / worker.py              日报与定时投递
events.py                          状态变更即时通知
tests/                             187 条，按 Day 分文件
```

更细的逐文件说明见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 九、卡住了怎么办

1. 先看 `docs/ARCHITECTURE.md` 里有没有写这个坑（大概率有，都是踩过的）
2. 长连接连不上 → 多半是系统代理掐了 TLS，报错会带 `SSL: UNEXPECTED_EOF_WHILE_READING`
3. 私聊发不出去、报 `230013` → 那个人不在应用可用范围，去后台加人再发版
4. 卡片点了没反应 → 回调配置 `card.action.trigger` 没配，或者改了配置没发布版本
5. 都不是，就在 Issue 里贴日志，把 `main.py` 打印的那几行带上
