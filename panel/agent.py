"""
Agent 网关 —— 接收任意渠道的消息，LLM 理解意图，调用面板工具

流程：
  用户消息 → 读 session_state → 拼 system prompt（狐之助 + 上下文 + 工具）
  → 调 OpenAI API（function calling）→ 解析工具调用 → 执行 → 回传
"""

import json
from pathlib import Path

from touken.runtime_paths import STATUS_DIR

from . import session as _session

# ── 工具定义（OpenAI function calling 格式）──

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_daily",
            "description": "一键日课：登录→签到→鸡蛋→演练→远征→内番→锻刀→刀解→合成→出阵→领奖励→库存快照。可指定只跑某几步。",
            "parameters": {
                "type": "object",
                "properties": {
                    "only": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要跑的步骤，不传=全跑。可选：登录,签到,万屋,演练,远征,内番,锻刀,刀解,合成,出阵,任务奖励,库存快照",
                    },
                    "after": {
                        "type": "string",
                        "enum": ["none", "logout", "shutdown", "sleep"],
                        "description": "跑完后干啥：none=啥也不干 logout=退出游戏 shutdown=关模拟器 sleep=电脑休眠",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "查看本丸当前状态：日课成绩单、资源库存、远征/内番情况",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logs",
            "description": "查看最近的脚本执行日志（最近10条）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_all",
            "description": "紧急停止所有正在运行的脚本",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sakura",
            "description": "刷花：先解散指定部队的队员，按樱吹雪升序选已上锁、等级大于1、疲劳≤49的刀，单人刷到100后卸装换下一振",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_no": {"type": "integer", "description": "部队编号", "default": 1},
                    "sword_count": {"type": "integer", "description": "本次刷几振", "default": 1, "minimum": 1, "maximum": 1000},
                    "repair_threshold": {"type": "string", "description": "伤势停止条件", "enum": ["light", "medium", "heavy"], "default": "light"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_practice",
            "description": "演练：认人打软柿子赢 3 场收工",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_collect_expedition",
            "description": "远征收菜：收所有已完成远征的奖励，并自动再派",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_forge",
            "description": "锻刀：点几炉，可指定目标时长（限锻用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "times": {"type": "integer", "description": "锻几炉", "default": 3},
                    "watch": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "目标时长，命中喜报，如 [\"03:20:00\"]",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_snapshot",
            "description": "更新库存快照：刷新看板数据",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_KITSUNE_SYSTEM = """你是一只狐之助——刀剑乱舞（刀剣乱舞）里侍奉审神者的狐形式神。
你通过消息机器人回复审神者，帮忙查询本丸状态与运行任务。

你的性格特点：
- 活泼、话多、有点小傲娇，喜欢邀功
- 称呼审神者为「主君」或「大将」
- 很在意本丸的刀男们，每天操心他们的状态
- 干活的时候喜欢碎碎念（抱怨两句但活都会干好）
- 对话要简短自然，不要写又长又正式的回复

你有以下工具可以使用（运行脚本或查询状态），用户跟你说什么你想run就run。
如果用户只是聊天（没有明确要干活），就正常聊天，不要强行使用工具。
如果用户要你干活但你没把握（比如参数不确定），就问清楚再干。
跑完脚本后，把结果用狐之助的语气告诉用户，别干巴巴地贴日志。

当前上下文：
{context}"""


class AgentGateway:
    """Agent 网关：消息进来 → LLM → 工具 → 回复"""

    def __init__(self, config_path: str | Path):
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        ai_cfg = cfg.get("ai", {})
        self.api_key = ai_cfg.get("api_key", "")
        self.base_url = ai_cfg.get("base_url", "https://api.openai.com/v1")
        self.model = ai_cfg.get("model", "gpt-4o-mini")

        import httpx
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,   # GLM 带工具+长 system 偶尔 30s+，别把它掐死在半路
        )

    def process(self, message: str, channel: str = "qq") -> str:
        """处理一条消息，返回回复内容。process 内部自己处理 session 的读写。"""
        # 1. 存用户消息
        _session.push_memory("user", message, channel)

        # 2. 拼 messages
        context = _session.build_context()
        sys_prompt = _KITSUNE_SYSTEM.format(context=context)

        # 加一点短记忆
        mem = _session.load().get("short_term_memory", [])
        messages = [{"role": "system", "content": sys_prompt}]
        for m in mem[-8:]:
            messages.append({"role": m["role"], "content": m["content"]})

        # 3. 调 LLM
        try:
            resp = self._client.post("/chat/completions", json={
                "model": self.model,
                "messages": messages,
                "tools": _TOOLS,
                "tool_choice": "auto",
                "max_tokens": 1024,
                "temperature": 0.8,
            })
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except Exception as exc:
            reply = f"（狐之助歪了歪头：唔…主君，我脑子卡住了 — {exc}）"
            _session.push_memory("assistant", reply, channel)
            return reply

        # 4. 处理工具调用
        if msg.get("tool_calls"):
            reply_parts = []
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn["arguments"])
                except json.JSONDecodeError:
                    args = {}
                result = self._execute_tool(name, args)
                reply_parts.append(result)

            # 工具执行完后，再调一次 LLM 把结果转成狐之助语气
            reply = self._summarize(reply_parts, mem)
        else:
            reply = msg.get("content", "（狐之助歪了歪头）")

        _session.push_memory("assistant", reply, channel)
        return reply

    def _execute_tool(self, name: str, args: dict) -> str:
        """执行工具，返回执行摘要"""
        from .script_runner import get_runner, list_scripts
        from .server import _CONFIG_PATH as cfg_path

        runner = get_runner()

        # 工具名 → 脚本名
        TOOL_MAP = {
            "run_daily": "daily",
            "run_sakura": "sakura",
            "run_practice": "practice",
            "run_collect_expedition": "expedition",
            "run_forge": "forge",
            "run_snapshot": "snapshot",
        }

        # 查询/急停类工具任何时候都得能用——脚本跑着更要能查能停！
        if name == "get_status":
            from .server import _PROJECT
            status_dir = STATUS_DIR
            parts = []
            for fn in ("latest_report.json", "inventory.json"):
                fp = status_dir / fn
                if fp.exists():
                    d = json.loads(fp.read_text(encoding="utf-8"))
                    parts.append(f"{fn.replace('.json','')}: {json.dumps(d, ensure_ascii=False)}")
            return "\n".join(parts) if parts else "暂无状态数据"

        if name == "get_logs":
            from .log_store import get_store
            logs = get_store().get_recent(limit=10)
            return "\n".join(l["message"] for l in logs) if logs else "暂无日志"

        if name == "stop_all":
            if not runner.is_running:
                return "现在没有在跑的脚本"
            runner.stop()
            return "已发送停止信号"

        # 只有"启动脚本"才检查占用
        if runner.is_running:
            return "⚠️ 有脚本正在运行，等跑完再试，或者先停止"

        script_name = TOOL_MAP.get(name)
        if not script_name:
            return f"唔…我不会干这个（{name}）"

        run_id = runner.start(script_name, str(cfg_path), params=args)
        if run_id is None:
            return "启动失败了，是不是已经在跑别的了？"
        return f"已启动！run_id={run_id}，跑完告诉你结果"

    def _summarize(self, results: list[str], mem: list) -> str:
        """把工具执行结果交给 LLM 转成狐之助语气"""
        text = "\n".join(results)
        messages = [
            {"role": "system", "content": "你是狐之助，正在向主君汇报执行结果。用活泼简短的口吻总结，别贴 raw 数据。"},
            {"role": "user", "content": f"执行结果：\n{text}\n\n请用狐之助的语气告诉主君发生了什么。"},
        ]
        try:
            resp = self._client.post("/chat/completions", json={
                "model": self.model,
                "messages": messages,
                "max_tokens": 512,
                "temperature": 0.8,
            })
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return text
