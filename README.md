# Trime 配置

Rime 输入法自定义配置，基于[万象拼音 pro](https://github.com/amzxyz/rime-wanxiang)，提供 **万象虎** 与 **小鹤双拼** 两套方案。

本仓库只保存「补丁」与「皮肤」，完整产物由 `build.py` 从上游拉取并叠加生成，避免把 40MB+ 的词库放进 git。

## 快速开始

```powershell
# 交互式选择方案（1. 万象虎  2. 小鹤双拼）
D:\Lib\Python31310\python.exe build.py

# 或直接指定方案
D:\Lib\Python31310\python.exe build.py --scheme wxh     # 万象虎
D:\Lib\Python31310\python.exe build.py --scheme flypy   # 小鹤双拼
```

需要联网（下载 upstream release 与词库），选择万象虎时还需要系统 `git`。
产物为 `Trime-wxh.zip` / `Trime-flypy.zip`，解压后整个目录即为 Rime 用户目录内容。

### 环境相关说明

- 脚本优先使用 `requests`，未安装时自动回落到标准库 `urllib`（当前环境走的是 urllib）。
- 沙箱/受限环境下需要放开网络访问，否则下载会因 TLS 失败而报错。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--scheme {wxh,flypy}` | 直接指定方案，跳过询问 |
| `-y, --yes` | 全程使用默认值（默认方案：万象虎） |
| `-o, --output PATH` | 指定输出 zip 路径 |
| `--workdir PATH` | 构建临时目录，默认 `.build/` |
| `--keep-workdir` | 保留临时目录，便于检查中间产物 |
| `--skip-download` | 复用临时目录里已下载的文件（调试用） |

## 构建流程

脚本按 `README` 描述的六步执行：

1. 询问方案（万象虎 / 小鹤双拼）。
2. 从 `amzxyz/rime-wanxiang` 最新 release 下载对应 zip 并解压到临时目录；**万象虎**额外 clone
   [`zhhwux/wxzhh`](https://github.com/zhhwux/wxzhh) 并覆盖上去（小鹤双拼不覆盖仓库内容）。
3. 复制 `patch/wxh/`（万象虎）或 `patch/flypy/`（小鹤双拼）到产物，覆盖同名文件。
4. 复制 `patch/custom_dict/` 到产物，并按其中的 `download.json` 下载词库到同一目录。
5. 复制 `skin/` 内所有文件到产物。
6. 打包为 zip 输出。

### 产物结构（关键文件）

```
Trime-wxh.zip / Trime-flypy.zip
├── default.custom.yaml          # 按键行为覆盖（来自 patch）
├── wanxiang_pro.custom.yaml     # 双拼/虎码与模糊音等配置（来自 patch）
├── wanxiang_pro.dict.yaml       # 覆盖上游, 额外导入 custom_dict/*
├── wanxiang_reverse.custom.yaml # 反查双拼修复
├── Ice_Mint.trime.yaml          # Ice Mint 主题（来自 skin）
├── xime.custom.yaml             # Xime 皮肤配置（来自 skin）
├── custom_dict/
│   ├── user.dict.yaml           # 个人词库
│   ├── minecraft_cn.dict.yaml   # 由 download.json 下载
│   └── download.json
└── ...                          # upstream release / 仓库 2 的其余文件
```

> `custom_dict/` 必须保持为子目录：`wanxiang_pro.dict.yaml` 通过 `custom_dict/user`、
> `custom_dict/minecraft_cn` 导入词库，因此下载的词库与 `user.dict.yaml` 同级放在该目录内。

### 版本记录

每次构建的版本信息写入 `.version/`：

- `.version/wxh.json` — 万象虎
- `.version/flypy.json` — 小鹤双拼

内容包括 release 标签与发布时间、`zhhwux/wxzhh` 最后一次提交的哈希、下载的词库清单、
产物文件数与大小、构建时间等。示例：

```json
{
    "scheme": "wxh",
    "release": { "asset": "rime-wanxiang-tiger-fuzhu.zip", "tag": "v17.9.9" },
    "wxh_repo": { "repo": "zhhwux/wxzhh", "commit": "e2490995e5e83d4b17a276be74281a9ab30718ee" }
}
```

## 目录说明

| 路径 | 说明 |
| --- | --- |
| `build.py` | 构建脚本 |
| `patch/wxh/` | 万象虎方案的配置覆盖 |
| `patch/flypy/` | 小鹤双拼方案的配置覆盖 |
| `patch/custom_dict/` | 个人词库目录 + `download.json` 下载清单 |
| `skin/` | Ice Mint 主题与 Xime 皮肤配置 |
| `img/` | README 中的主题预览图 |
| `.version/` | 构建版本记录（脚本自动生成） |

## 功能特性

- 万象虎：拼音 + 虎码，整句输入，支持虎句 / 虎词 / 虎单切换与拆分提示
- 小鹤双拼：万象拼音 pro + 小鹤双拼键位
- 前后鼻音模糊音（`en/eng`、`in/ing`）
- 字词预测（默认关闭）

## 按键行为

- Caps Lock：清除已有输入
- Shift：不再控制中英切换，改用 Ctrl
- 默认英文标点，`Ctrl+.` 切换

## 使用方式

1. 运行 `build.py` 生成 zip（或让 CI 生成）。
2. 解压 zip，把内容复制到 Rime 用户目录。
3. 重新部署 Rime 生效。

## 主题预览

浅色模式：

![浅色模式](img/light_new.jpg)

深色模式：

![深色模式](img/dark_new.jpg)

## 鸣谢

- [万象拼音](https://github.com/amzxyz/rime-wanxiang) / [万象虎码](https://github.com/zhhwux/wxzhh)
- 皮肤预览和编辑工具：[edit4trime](https://hero20072.github.io/edit4trime/)
- Rime 输入法官方：[rime.im](https://rime.im/)
- 皮肤借鉴：[chwt163/mytrime](https://github.com/chwt163/mytrime)
