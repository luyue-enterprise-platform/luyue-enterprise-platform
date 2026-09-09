# -*- coding: utf-8 -*-
"""MCP 服务端模块 — 把平台业务能力以 MCP 工具形式对外暴露（v2.2.0 新增）

定位：MCP **服务端**。把平台已有的三类业务能力包装为标准 MCP 工具，供
Claude Desktop / WorkBuddy 等外部 AI 客户端通过 HTTP 调用，使平台成为
「可被 AI 驱动」的业务系统。

对外能力：
- 社保智能核算（重点群体参保统计与台账生成）
- PDF / Word 双向转换
- 劳动合同整理（按花名册智能重命名归档）

设计要点：
1. 传输：Streamable HTTP。单端点 POST /mcp，JSON-RPC 2.0，响应 application/json。
   不引入 mcp SDK（starlette/pydantic/httpx 等），仅用 stdlib + Flask——
   零新增依赖，避免给已验证的 PyInstaller 打包链增加变量。
2. 安全：仅回环地址可达（Flask 已绑定 127.0.0.1）+ Bearer Token 鉴权；
   令牌持久化在数据目录，可在门户页查看/重新生成；总开关可一键停用。
3. 异步：业务处理耗时较长，统一采用「提交→轮询状态→取结果」三步式，
   与平台现有的后台任务机制一致（社保直接复用其原生任务表）。
"""
__version__ = '1.0.0'
