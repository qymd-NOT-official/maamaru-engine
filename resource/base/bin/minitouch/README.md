minitouch（自研触摸注入用的设备端小二进制）

来源：openstf/minitouch（Apache-2.0）
https://github.com/openstf/minitouch

x86_64 / arm64-v8a 两个预编译版本来自 MaaAgentBinary 的转售包
（与上游发布物一致），仅作再分发，无任何修改。

用途：刀剑乱舞部分页面（刀帐竖向网格、一览横向翻页）不认
`input swipe` 注入的触摸事件，只认内核级注入。minitouch 直接写
/dev/input 设备节点，事件形态与真实手指一致。宿主端见
touken/minitouch_client.py。
