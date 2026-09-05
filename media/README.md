# 媒体文件目录

将歌曲、封面图片和 UTF-8 编码的歌词文件放在此目录中。

每个文件名都必须在上一级目录的 `music.json` 中被引用。对于 Android 4.2 客户端，推荐使用 MP3 音频、JPEG 或 PNG 封面，以及 UTF-8 编码的 TXT 歌词。

歌词是可选的。没有歌词时，将 `music.json` 中对应歌曲的 `lyrics` 设置为空字符串：

```json
"lyrics": ""
```

音频文件是必需的。仅把 MP3 复制到本目录不会自动发布歌曲，还必须在 `music.json` 对应播放列表的 `songs` 数组中添加歌曲对象，并增加该播放列表的 `version`。

注意：默认开启的「缓存清理」定时任务会删除本目录下所有未被 `music.json` 引用的文件，包括本说明文件。不要把备份或其他用途的文件放在这里，或者在 `miku.conf` 中设置 `cache_cleanup_enabled = false` 关闭该任务。详见上一级目录 `README.md` 的第 14.2 节。
