# 1. Every device is an MCP server ("node")

**Status:** accepted, 2026-09-14

## Context
Sommus should grow from one laptop to speakers, a phone, a Raspberry Pi per room and hardware I build
(ESP32 speaker, robot arm). If the brain knew how each device works, every new device would mean
changing the brain.

## Decision
Each device is a separate MCP server that declares its tools (name, schema, and a read-only /
destructive annotation). The brain launches nodes over stdio, lists their tools, and routes calls by
name. Interfaces (terminal, Telegram, voice) only exchange text and events with the brain.

## Consequences
- A new device is a new node; the brain doesn't change. Five nodes exist today: laptop (47 tools),
  vault, gmail, web and claude.
- Permission tiers come from each tool's MCP annotations, so safety is declared next to the code
  that does the thing. An unannotated tool is treated as destructive.
- The same nodes work from Claude Code and other MCP clients, which made them testable before the
  brain existed.
- A node that fails to start is reported and skipped; the rest keep working.
- Cost: a process per node, and stdio doesn't cross machines. The cloud-brain plan moves nodes to
  MCP over HTTP on a private network.
