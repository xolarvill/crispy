"""Crispy video editing MCP. Run: uv run python -m app.mcp.videowright --actor codex."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, ImageContent, Tool, ToolAnnotations
from app.data.session import SessionLocal, init_db
from app.services.video_tools import TOOLS, call_tool


def definitions():
    return [Tool(name=name, description=spec.description, inputSchema=spec.args.model_json_schema(),
                 annotations=ToolAnnotations(readOnlyHint=not spec.write)) for name, spec in TOOLS.items()]


async def serve(actor: str):
    init_db()
    server = Server("crispy-videowright")

    @server.list_tools()
    async def list_tools():
        return definitions()

    @server.call_tool()
    async def execute(name: str, arguments: dict):
        try:
            def invoke():
                with SessionLocal() as db:
                    return call_tool(db, name, arguments, "agent:" + actor)
            result = await asyncio.to_thread(invoke)
            content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
            if result.get("image_path"):
                path = Path(result["image_path"])
                content.append(ImageContent(type="image", mimeType="image/png" if path.suffix == ".png" else "image/jpeg",
                                            data=base64.b64encode(path.read_bytes()).decode()))
            return CallToolResult(content=content, structuredContent=result)
        except (ValueError, OSError) as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description="Crispy local video editing tools")
    parser.add_argument("--actor", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.actor[:100]))


if __name__ == "__main__":
    main()
