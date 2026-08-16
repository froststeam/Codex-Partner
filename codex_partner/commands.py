"""Browser slash-command catalog and input parsing."""

import shlex

from fastapi import HTTPException


SLASH_COMMANDS = [
    {"name": "model", "args": "[model] [effort]", "description": "选择模型和推理强度", "category": "session"},
    {"name": "fast", "args": "[on|off]", "description": "切换快速服务层", "category": "session"},
    {"name": "ide", "args": "[path]", "description": "打开当前网页工作区上下文", "category": "context"},
    {"name": "permissions", "aliases": ["approvals", "yolo"], "args": "[yolo|controlled|profile]", "description": "选择 Codex 权限和 YOLO 模式", "category": "session"},
    {"name": "keymap", "args": "", "description": "查看浏览器输入快捷键", "category": "browser"},
    {"name": "vim", "args": "[on|off]", "description": "切换输入框 Vim 模式标记", "category": "browser"},
    {"name": "experimental", "args": "[feature] [on|off]", "description": "查看或切换实验功能", "category": "codex"},
    {"name": "approve", "args": "", "description": "重试最近一次自动审查拒绝", "category": "thread"},
    {"name": "memories", "aliases": ["memory"], "args": "[on|off|status]", "description": "配置当前线程记忆或打开记忆面板", "category": "context"},
    {"name": "skills", "args": "[reload]", "description": "列出可用 Skills", "category": "context"},
    {"name": "import", "args": "", "description": "检测可导入的外部 Agent 配置", "category": "codex"},
    {"name": "hooks", "args": "", "description": "列出当前工作区 Hooks", "category": "codex"},
    {"name": "review", "args": "[instructions]", "description": "在当前线程发起代码审查", "category": "thread"},
    {"name": "rename", "args": "<name>", "description": "重命名当前线程", "category": "thread"},
    {"name": "new", "args": "[first message]", "description": "新建默认 YOLO 会话", "category": "thread"},
    {"name": "archive", "args": "", "description": "归档当前会话", "category": "thread", "destructive": True},
    {"name": "unarchive", "args": "", "description": "恢复已归档会话", "category": "thread"},
    {"name": "delete", "args": "", "description": "将当前会话移到回收站", "category": "thread", "destructive": True},
    {"name": "resume", "args": "[message]", "description": "继续当前会话", "category": "thread"},
    {"name": "fork", "args": "[message]", "description": "派生当前会话", "category": "thread"},
    {"name": "init", "args": "", "description": "在工作区创建 AGENTS.md 指令文件", "category": "context"},
    {"name": "compact", "args": "", "description": "压缩当前线程上下文", "category": "thread"},
    {"name": "plan", "args": "[on|off]", "description": "切换 Plan 协作模式", "category": "session"},
    {"name": "goal", "args": "[objective|clear|status|budget]", "description": "设置、查看和控制长期 Goal", "category": "goal"},
    {"name": "agent", "args": "", "description": "查看当前线程的 Agent 活动", "category": "thread"},
    {"name": "side", "args": "[message]", "description": "派生一个侧边会话", "category": "thread"},
    {"name": "copy", "args": "", "description": "复制最近一条 Codex 回复", "category": "browser"},
    {"name": "raw", "args": "[on|off]", "description": "切换原始工具事件显示", "category": "browser"},
    {"name": "diff", "args": "", "description": "显示工作区 Git 变更", "category": "context"},
    {"name": "mention", "args": "[path]", "description": "把工作区文件加入下一条消息", "category": "context"},
    {"name": "status", "aliases": ["account"], "args": "", "description": "显示线程配置、Goal 和账号状态", "category": "session"},
    {"name": "title", "args": "[on|off]", "description": "切换浏览器标题中的会话名", "category": "browser"},
    {"name": "statusline", "args": "", "description": "打开会话状态检查器", "category": "browser"},
    {"name": "theme", "args": "[dark|light]", "description": "切换浏览器主题", "category": "browser"},
    {"name": "mcp", "args": "[verbose]", "description": "列出已配置 MCP 工具", "category": "codex"},
    {"name": "plugins", "aliases": ["apps"], "args": "", "description": "列出 Codex 插件和 Apps", "category": "codex"},
    {"name": "logout", "args": "", "description": "退出 Codex 账号", "category": "account", "destructive": True},
    {"name": "exit", "aliases": ["quit"], "args": "", "description": "断开当前网页实时连接", "category": "browser"},
    {"name": "feedback", "args": "[message]", "description": "显示反馈入口信息", "category": "codex"},
    {"name": "ps", "args": "", "description": "列出当前线程后台终端", "category": "thread"},
    {"name": "stop", "args": "", "description": "停止当前 turn 或后台终端", "category": "thread", "destructive": True},
    {"name": "clear", "args": "", "description": "清空当前视图并新建默认 YOLO 会话", "category": "thread", "destructive": True},
    {"name": "personality", "args": "[none|friendly|pragmatic]", "description": "选择 Codex 沟通风格", "category": "session"},
    {"name": "subagents", "args": "", "description": "查看当前线程的子 Agent 活动", "category": "thread"},
    {"name": "help", "args": "[command]", "description": "显示网页支持的斜杠命令", "category": "browser"},
]

SLASH_COMMAND_BY_NAME = {command["name"]: command for command in SLASH_COMMANDS}
SLASH_ALIASES = {
    alias: command["name"]
    for command in SLASH_COMMANDS
    for alias in command.get("aliases", [])
}


def parse_slash_command(raw: str) -> tuple[str, str, list[str]]:
    """Normalize aliases and parse shell-style command arguments."""
    text = raw.strip()
    if not text.startswith("/"):
        raise HTTPException(400, "Slash commands must start with /")
    head, _, arg_text = text[1:].partition(" ")
    name = SLASH_ALIASES.get(head.strip().lower(), head.strip().lower())
    if name not in SLASH_COMMAND_BY_NAME:
        raise HTTPException(400, f"Unknown slash command: /{head}")
    try:
        args = shlex.split(arg_text)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid command arguments: {exc}") from exc
    return name, arg_text.strip(), args
