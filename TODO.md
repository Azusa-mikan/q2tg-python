# 已支持消息/事件类型（双向）

- 普通文字消息
- 回复
- 图片消息
  - 支持带字（caption）
  - OneBot 发往 Telegram 时，同一条消息中的 2～10 张图片会作为图片组发送；若任一图片超过 10 MB，则整组作为文件组发送
  - OneBot 发往 Telegram 时，单张图片超过 10 MB 会作为文件发送
  - 单个图片超过 20 MB 时提示不转发
- 视频
  - Telegram 发往 OneBot 时，非 H.264 视频或包含非 AAC 音轨的视频会转码
  - 单个视频超过 20 MB 时提示不转发
- 文件
  - 单个文件超过 20 MB 时提示不转发
- 语音
  - 单个语音超过 20 MB 时提示不转发
  - OneBot 发往 Telegram 时会规范化为 Ogg/Opus；已经是 Ogg/Opus 的语音不会重复转码
- 贴纸
  - Telegram 静态贴纸作为图片发送，视频贴纸转为 GIF 发送
  - Telegram TGS 动态贴纸转为 GIF 发送
    - 转换后的 GIF 不限制大小
  - OneBot 侧以图片段收到的 GIF 会作为 Telegram 动画发送
- 事件
  - 消息被撤回（群聊）
  - 成员加入
  - 成员退出
  - 成员禁言
  - 戳一戳
- 撤回
  - 用户在 Telegram 回复目标消息后使用 `/undo` 发起
  - 命令会撤回 Telegram 和 OneBot 两侧的对应消息
  - Onebot 侧正常撤回即可

Telegram 媒体组最多支持 10 项、合计 100 MB；其中每项仍受 20 MB 上限限制。

# 待支持消息类型

- 闪照
- 小表情（Q Face）
- 合并转发
- 精华消息（TG的置顶消息）
  - 不保证其它 Onebot 实现支持（反正 SnowLuma 支持）
