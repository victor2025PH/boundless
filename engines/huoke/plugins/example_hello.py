"""
Example OpenClaw Plugin — Hello World.
Demonstrates plugin lifecycle hooks and metadata.
"""
import logging

log = logging.getLogger("openclaw.plugins.example_hello")


def plugin_info():
    return {
        "version": "1.0.0",
        "author": "OpenClaw Team",
        "description": "示例插件 — 演示插件生命周期和钩子函数",
    }


def on_load():
    log.info("[HelloPlugin] loaded and ready!")


def on_unload():
    log.info("[HelloPlugin] unloaded, bye!")


def on_device_connected(device_id: str = "", **kw):
    log.info("[HelloPlugin] device connected: %s", device_id)


def on_task_complete(task_id: str = "", status: str = "", **kw):
    log.info("[HelloPlugin] task %s finished with status: %s", task_id, status)
