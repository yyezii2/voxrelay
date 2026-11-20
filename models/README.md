# 放自己的音色模型

仓库不附带任何音色模型。这个目录是给你放自己模型的，目录名就是音色 id。
服务启动时自动扫描，不需要改 `config.json`。

## GPT-SoVITS（推荐，零样本不用训练）

```
models/gsv/<音色id>/
    ref.wav       必需。5~10 秒干净人声，别带 BGM，决定音色像不像
    prompt.txt    必需。ref.wav 里说的那句话，原样转录
    *.ckpt        可选。GPT 权重，有就会自动切换
    *.pth         可选。SoVITS 权重
    voice.json    可选。覆盖显示名等信息
```

只放 `ref.wav` + `prompt.txt` 就是**零样本克隆**，不用训练，效果取决于参考音频质量。
额外放了 `.ckpt` / `.pth` 就按微调模型跑（更稳，但要你有权重）。

`voice.json` 可选字段：

```json
{
  "name": "显示用的名字",
  "desc": "一句话描述",
  "prompt_lang": "zh",
  "text_lang": "zh",
  "language": "zh-CN",
  "speed_factor": 1.0
}
```

> `prompt_lang` 是参考音频的语言，`text_lang` 是要合成的语言。参考音频是日语、
> 要念中文，就写 `"prompt_lang": "ja", "text_lang": "zh"`。

示例：

```
models/gsv/myvoice/
    ref.wav
    prompt.txt
    myvoice_e10_s300.pth
    myvoice-e15.ckpt
    voice.json
```

## RVC（音色转换，需要 RVC worker）

```
models/rvc/<模型名>/
    *.pth         必需
    *.index       可选，检索特征库，有的话更像
    voice.json    可选
```

扫到之后会注册成 `rvc-<模型名>`，底层用 edge 合成再转音色。
`voice.json` 可指定 `base_voice`（底嗓，默认 `zh-CN-XiaoxiaoNeural`）和 `pitch`（半音）。

RVC 需要 `rvc.enabled = true` 且 worker 跑起来（默认 `http://127.0.0.1:8790`）。

## 注意

- 模型体积大，**不要提交到 git**（`.gitignore` 已经排除这两个目录）。
- 音色版权是你自己的责任。用别人的角色声音做模型再分发，风险不在代码这边。
