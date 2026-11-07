# v0.9.7 最小叠层：计时、记忆失败、确认式遗忘

父提交是标签 `v0.9.7`，哈希 `87c6df77b0220ddbd8fc804f984f83f69f3225e5`。不要把这个分支合并进 `main`。

这个补丁不改 `config.yaml`，也不包含 `data/.config.yaml`。人设、音色和本机覆盖配置保持原文件。`agent_id` 默认仍是 `rocky`。

## 现场文件怎么叠

已对照过的现场文件：

- `core/handle/receiveAudioHandle.py`：保留 FunASR `content` 解析。记忆查询不提前创建任务。
- `core/providers/tts/base.py`：保留引号安全切句。两处被误打码的调用恢复为 v0.9.7 的 `textUtils.get_string_no_punctuation_or_emoji`。
- `core/providers/tts/edge.py`：与 v0.9.7 相同，只多记提供者首个音频字节。
- `core/providers/memory/mem0_selfhost/mem0_selfhost.py`：按现场的自建接口改写，不再把响应正文写进日志。
- `plugins_func/functions/forget_memory.py`：按现场的搜索加删除流程收紧。本机若已有同名文件，部署前先对照备份，不要和别的插件一起覆盖。

已核对与 v0.9.7 一致、因此只加计时的文件：`connection.py`、`sendAudioHandle.py`、`listenMessageHandler.py`、`asr/base.py`、`vad/silero.py`。没有改 `memory/base.py`、`mem0ai.py`、`plugins_func/register.py`。

上传的 `edge.py` 相对 v0.9.7 只有文件末尾换行，没有单独的放大链路。`base.py` 相对 v0.9.7 的实质差别是引号安全切句。

## 计时

阶段：`end_of_speech`、`asr_final`、`memory_lookup_start`、`memory_lookup_end`、`llm_request`、`llm_first_token`、`llm_first_sentence`、`tts_request`、`tts_first_audio`、`server_audio_sent`。

`tts_first_audio` 是 TTS 提供者产出的第一段音频。`server_audio_sent` 是服务器把第一包交给套接字。两者都不是设备喇叭已经播出。摘要里的 `hardware_playback=unmeasured` 表示没有设备播放时刻。

记忆搜索花了多久，这些日志就记多久。搜索本身没有变短，不能据此说语音变快了。

## 记忆失败

查询仍在 `chat()` 里才开始，和 v0.9.7 一样从工作线程交给事件循环。识别结束时不创建任务。提供者如果在协程里同步阻塞，超时取消不了这次调用；工作线程到达 `memory_query_timeout_seconds`（代码默认 15，可在本机 `data/.config.yaml` 覆盖，本补丁不写这个文件）后会丢弃晚到的结果。

HTTP 非 200、传输失败、返回值不是字符串：不把空字符串交给模型，并说明这次不回答，以免把故障说成没有记忆。HTTP 200 且没有条目仍然是空记忆，模型会照常回答。

自建客户端按事件循环复用一个 `httpx.AsyncClient`。保存记忆若跑在另一个事件循环里，会用那个循环自己的客户端，不会把客户端跨循环共用。

## 遗忘

预览只搜索 `user_id` 为当前 `role_id` 的候选。删除必须同时满足：编号来自这次预览、口令一致、`GET /memories/{id}` 返回的 `user_id` 相同。不满足就没有 `DELETE`。日志不包含记忆正文。

如果自建服务没有 `GET /memories/{id}`，删除会全部拒绝。这是未关闭的验收项，不能当成已经能在现场忘掉一条记忆。

## 还不能算验收通过

- 没有在 Mac 上部署，也没有做现场语音或串口验收。
- 没有访问 `:8888`。所有 HTTP 测试都是假传输。
- 云端 Python 是 3.10.18 的单元测试，不是现场进程。
- 旧的 TTS 成功日志仍可能带句子文本；新计时行不带。
- 任务投递不在这个补丁里。
- 现场 `config.yaml` 的 Taky 提示词没有收到，也没有改。
