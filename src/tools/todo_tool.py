"""Todo tool — task checklist management for the agent."""

import json
import logging
import threading
from pathlib import Path
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

from core.config import AGENT_HOME
_TODO_FILE = AGENT_HOME / "todos.json"
# Load-modify-save on one shared file: parallel batch children would clobber
# each other's writes without serialization.
_TODO_LOCK = threading.Lock()


def _load_todos() -> dict:
    if _TODO_FILE.exists():
        try:
            return json.loads(_TODO_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_todos(data: dict):
    _TODO_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TODO_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _scoped_list_name(args: dict, kw: dict) -> str:
    """Namespace list names by session — a subagent's checklist must never
    land in the main agent's lists. No context (tests) → bare name."""
    session_key = (kw.get("context") or {}).get("session_key")
    name = args.get("list", "default")
    return f"{session_key}::{name}" if session_key else name


def _check_todo() -> bool:
    return True


def _todo(args: dict, **kw) -> str:
    action = args.get("action", "list")
    list_name = _scoped_list_name(args, kw)
    if action == "list":
        return _list_todos(list_name)
    elif action == "add":
        return _add_todo(list_name, args)
    elif action == "update":
        return _update_todo(list_name, args)
    elif action == "delete":
        return _delete_todo(list_name, args)
    else:
        return tool_error(f"Unknown action: {action}. Use: list, add, update, delete")


def _list_todos(list_name: str) -> str:
    with _TODO_LOCK:
        todos = _load_todos()
    items = todos.get(list_name, [])
    return tool_result(list=list_name, items=items, count=len(items))


def _add_todo(list_name: str, args: dict) -> str:
    title = args.get("title", "")
    description = args.get("description", "")
    priority = args.get("priority", "medium")

    if not title:
        return tool_error("title is required")

    with _TODO_LOCK:
        todos = _load_todos()
        items = todos.get(list_name, [])
        item = {
            "id": f"todo_{len(items) + 1}",
            "title": title,
            "description": description,
            "priority": priority,
            "status": "pending",
        }
        items.append(item)
        todos[list_name] = items
        _save_todos(todos)
    return tool_result(success=True, item=item)


def _update_todo(list_name: str, args: dict) -> str:
    item_id = args.get("id", "")
    status = args.get("status", "")

    if not item_id:
        return tool_error("id is required")

    with _TODO_LOCK:
        todos = _load_todos()
        items = todos.get(list_name, [])

        for item in items:
            if item["id"] == item_id:
                if status:
                    item["status"] = status
                if "title" in args:
                    item["title"] = args["title"]
                if "description" in args:
                    item["description"] = args["description"]
                if "priority" in args:
                    item["priority"] = args["priority"]
                _save_todos(todos)
                return tool_result(success=True, item=item)

    return tool_error(f"Item not found: {item_id}")


def _delete_todo(list_name: str, args: dict) -> str:
    item_id = args.get("id", "")

    if not item_id:
        return tool_error("id is required")

    with _TODO_LOCK:
        todos = _load_todos()
        items = todos.get(list_name, [])
        new_items = [i for i in items if i["id"] != item_id]

        if len(new_items) == len(items):
            return tool_error(f"Item not found: {item_id}")

        todos[list_name] = new_items
        _save_todos(todos)
    return tool_result(success=True, deleted=item_id)


registry.register(
    name="todo",
    schema={
        "type": "function",
        "function": {
            "name": "todo",
            "description": (
                "Manage task checklists. Create, update, and track todo items. "
                "Organize items into named lists. Use for tracking multi-step tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "update", "delete"],
                        "description": "Action to perform (default: list)",
                    },
                    "list": {"type": "string", "description": "Todo list name (default: default)"},
                    "title": {"type": "string", "description": "Item title (for add)"},
                    "description": {"type": "string", "description": "Item description"},
                    "id": {"type": "string", "description": "Item ID (for update/delete)"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "Item status",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Item priority (default: medium)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    handler=lambda args, **kw: _todo(args, **kw),
    check_fn=_check_todo,
    toolset="base",
)


def hydrate_from_history(messages: list[dict]):
    """Recover todo state from conversation history.

    The gateway creates a fresh agent per message, so the file-based
    todo store is already persistent. But if the file was lost or
    this is a different machine, we can recover from the most recent
    todo tool response in the conversation history.
    """
    # Scan backwards for the most recent todo tool response
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                continue
            # Check if this looks like a todo tool response
            items = parsed.get("items")
            list_name = parsed.get("list", "default")
            if items and isinstance(items, list):
                todos = _load_todos()
                # Only hydrate if the list is currently empty
                if not todos.get(list_name):
                    todos[list_name] = items
                    _save_todos(todos)
                    logger.info("Hydrated todo list '%s' from history (%d items)",
                                list_name, len(items))
                return
        except (json.JSONDecodeError, TypeError):
            continue
