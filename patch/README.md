后面由AI重写README

[Releases · amzxyz/rime-wanxiang](https://github.com/amzxyz/rime-wanxiang/releases)

下载rime-wanxiang-tiger-fuzhu.zip -> 1

[zhhwux/wxzhh: 万象虎码整句方案](https://github.com/zhhwux/wxzhh)

下载整个仓库 -> 2

[Yxz12-ye/My_Rime: 个人的输入方案和皮肤](.)

(就是本文件夹) -> 3

你要做的: 

1. 用脚本询问用户方案(1.万象虎 2.小鹤双拼)

2. 如果选择**万象虎**, 下载仓库2, 还有1里面的rime-wanxiang-tiger-fuzhu.zip, 接下来把1解压到临时目录(并记录release版本到脚本所同目录.version/, 用什么文件, 怎么记录随意, 仓库2也要记录, 记录最后一次提交的哈希), 将仓库2内文件放入临时目录并覆盖, 到的产物记录为4, 如果选择小鹤双拼, 那么就下载rime-wanxiang-flypy-fuzhu.zip, 然后解压, 但是不要覆盖仓库2的东西, 产物记录为4

3. 如果上一步选择1, 那么就复制patch/wxh内所有东西到产物4中(需要覆盖), 选择2就复制/patch/origin到4

4. 复制patch/custom_dict/文件夹到4, 根据patch/custom/download.json下载词库文件到同目录(也就是和user.dict.yaml同级)

5. 复制skin内所有文件到产物4

6. 最后把产物4打包输出(zip)