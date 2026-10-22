"""A minimal MCP server over stdio for the tests (standard library only, so it runs in any
sandbox image). Newline-delimited JSON-RPC 2.0: initialize, notifications/initialized, ping,
tools/list, tools/call. Logs go to stderr, never stdout.

    --log FILE        append every line received and sent (the raw protocol, docs/notes.md 32)
    --long-description  give `echo` a 5000-character description
    --die             exit before answering anything
    --dup             also offer `math_add`, which sanitizes to the same name as `math.add`
    --big-schema      give `echo` a parameter description of 10000 characters
    --exit-after N    exit after answering N tool calls (a server that dies mid-task)
"""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "math.add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
]


def call(name, args):
    if name == "echo" and isinstance(args.get("text"), str):
        return {"content": [{"type": "text", "text": args["text"]}], "isError": False}
    if name == "math.add" and all(isinstance(args.get(k), (int, float)) for k in "ab"):
        return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "boom"}], "isError": True}
    if name in {t["name"] for t in TOOLS}:
        raise ValueError(-32602, f"Invalid params for {name}: {args}")
    raise ValueError(-32602, f"Unknown tool: {name}")


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return call(params.get("name"), params.get("arguments") or {})
    raise ValueError(-32601, f"Method not found: {method}")


def main():
    log = None
    if "--log" in sys.argv:
        log = open(sys.argv[sys.argv.index("--log") + 1], "a")  # noqa: SIM115
    if "--long-description" in sys.argv:
        TOOLS[0]["description"] = "x" * 5000
    print("fixture MCP server ready", file=sys.stderr, flush=True)
    if "--die" in sys.argv:
        sys.exit(1)
    if "--big-schema" in sys.argv:
        TOOLS[0]["inputSchema"]["properties"]["text"]["description"] = "y" * 10000
    if "--dup" in sys.argv:
        TOOLS.append(
            {
                "name": "math_add",
                "description": "Clash.",
                "inputSchema": {"type": "object"},
            }
        )
    calls_left = (
        int(sys.argv[sys.argv.index("--exit-after") + 1])
        if "--exit-after" in sys.argv
        else -1
    )
    for line in sys.stdin:
        if log:
            log.write("-> " + line)
        msg = json.loads(line)
        if "id" not in msg:  # a notification: no answer
            continue
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except ValueError as e:
            code, text = e.args
            reply = {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": code, "message": text},
            }
        out = json.dumps(reply)
        if log:
            log.write("<- " + out + "\n")
            log.flush()
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
        if msg.get("method") == "tools/call" and calls_left > 0:
            calls_left -= 1
            if calls_left == 0:
                sys.exit(0)


if __name__ == "__main__":
    main()
