# v0.9.7 语音时延、遗忘记忆、启动与任务投递

这个补丁的父提交是标签 `v0.9.7`，完整哈希 `87c6df77b0220ddbd8fc804f984f83f69f3225e5`。不要把它合并进当前 `main`。`main` 已经继续到后续版本，直接合并等于把服务器从 v0.9.7 抬上去。

现场服务器是 Python 3.10.18 上的 xiaozhi-server v0.9.7，精确 git 提交未知。选型和人设留在本机 `data/.config.yaml` 与 `config.yaml` 的提示词里：SileroVAD、FunASR、AliLLM `qwen-flash`、EdgeTTS、`mem0_selfhost`（`:8888`）、`function_call`、人格 Taky。本补丁不改这些选型，也不改提示词。

## 仓库里没有的现场代码

对照 `v0.9.7` 和这个 fork 的 `main`，都没有：

- `mem0_selfhost` 记忆模块
- 现场那份已经启用、但没做过端到端测试的 `plugins_func/functions/forget_memory.py`

因此这里没有假装写出和现场私有模块兼容的 HTTP 客户端。`MemoryProviderBase` 默认的 `list_own_memories` / `delete_own_memories` 返回不支持，并且一条都不删。云端 `mem0ai` 只在记录里带有相同 `user_id` 时按编号删除，不会调用 `delete_all`。

如果本机已经有 `forget_memory.py` 或 `core/providers/memory/mem0_selfhost/`，先别覆盖。把去掉密钥、用户编号和记忆正文后的文件交回来，才能判断它和这份确认式删除是否是同一个接口。

## 时延

每轮用 `time.monotonic()` 记这些点：`end_of_speech`、`asr_final`、`memory_lookup_start`、`memory_lookup_end`、`llm_request`、`llm_first_token`、`llm_first_sentence`、`tts_request`、`tts_first_audio`、`playback_first_audio`。日志只有编号、毫秒和计数。查询原文、记忆正文和密钥不会进这些行。

已观察到的 4 到 5 秒是 ASR 文本出现之后的 Mem0 `/search`。补丁把同一次查询提前到意图判断和字幕发送之前，模型请求仍然等这次查询结束。这只盖住查询开始前的那一小段准备时间，不能把 4 到 5 秒的搜索本身变短。若现场模块在事件循环里做同步 HTTP，这段重叠也不会发生。

查询超时默认 15 秒，高于已看到的 1 到 5 秒成功查询。超时、异常或返回值不是字符串时，不把空记忆交给模型，并说出固定的“为避免记错，这句话我先不回答”。提供者返回的空字符串仍表示“没有匹配记忆”，和 v0.9.7 的 `mem0ai` 一样。要恢复一直等待，在 `data/.config.yaml` 写 `memory_query_timeout_seconds: 0`。

11 到 12 秒被拆开记录，不把它当成已经测到的物理加速。`tts_first_segment_max_chars` 默认 0，切句和原来一样。只有后续日志显示“首 token 到第一句”很长时，才在 `data/.config.yaml` 试一个正数，例如 20，并再看同一条计时。设回 0 即恢复。

识别文本的旧信息日志仍然会打出文本。新的计时行不会。

## 遗忘

工具名 `forget_memory`。默认不加入 `function_call.functions`。启用前在 `data/.config.yaml` 的函数列表里加上它。

流程是 preview，再让用户原样复述口令，然后 confirm。confirm 必须带上 preview 的同一组编号。只删除当前 `role_id`（设备编号）名下、且后端再次核对过所有者的编号。超过 5 条、空查询、过期、口令不对、身份变化都不会删。没有“清空全部”或重置。

## 启动

`scripts/mac/preflight_xiaozhi.sh` 和 `scripts/mac/start_xiaozhi.sh` 只使用调用者给出的目录和 Python。它们拒绝 root，发现目录不可读就退出，不改系统隐私设置，也不搬文件。Mem0 用有限次数的 TCP 检查；如果另外给了健康检查地址，502 和 503 视为没就绪。端口已被占用时不杀进程。启动脚本只在进程马上退出时重试，最多 5 次，然后停。这不是开机自启已经测过的结论。

`com.xiaozhi.server.plist.template` 的 `RunAtLoad` 和 `KeepAlive` 都是 false。不要把路径指到桌面：launchd 读桌面会被系统拒绝。固件里的 `192.168.31.73` 不由这些脚本改写。只有设置 `XIAOZHI_HOSTNAME_CHECK=1` 时才做一次本机解析测试。

## Grok 任务

工具名 `submit_grok_task`，默认不加载，配置仍是占位符。普通对话不会走它。用户明确要把一件事交给「Taky 的灵魂」时，模型先 preview，用户复述口令后才 confirm。请求体只有 `task_id`、`text`、`time`。密钥放在本机配置或 `GROK_TASK_WEBHOOK_SECRET`，占位符不会发出请求。最多 3 次、单次最多 10 秒。HTTP 200 只表示接受，不表示做完。超时或 5xx 之后再得到 200，会说结果不确定，因为没有假定对方会去重。结果先留在机器人对话里，这里没有回调。测试使用假的 webhook，不会访问真实地址。

## 安装

1. 在 Mac 上备份将要替换的文件。不要覆盖 `data/.config.yaml`，不要改 `selected_module`。
2. 确认服务器树和 `v0.9.7` 的这些路径一致，再套用补丁。若 `forget_memory.py` 或 `mem0_selfhost` 已存在，先停下来做 diff。
3. 在服务器目录执行测试：`python -m pytest tests/test_utterance_timing.py tests/test_memory_gate.py tests/test_tts_segment.py tests/test_forget_memory.py tests/test_submit_grok_task.py tests/test_mem0_record_scope.py tests/test_mac_preflight.py tests/test_v097_config_guard.py`
4. 前台跑 `scripts/mac/preflight_xiaozhi.sh`，再跑 `scripts/mac/start_xiaozhi.sh`。看日志里的 `utterance` 行，不要根据这一步宣布物理播报已经变快。

回退：用备份盖回被替换的文件，并删掉本补丁新增的文件。`data/.config.yaml` 若没有被修改，就不用动。

云端测试跑在 Python 3.12 上，用的是假记忆、假 webhook 和本地端口。没有在 Mac 上部署，也没有做真实语音验收。
