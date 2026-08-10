# Audit celah backend vs frontend — Nalar.ai

Backend: 79 path / 113 operasi (snapshot openapi_snapshot.json).
Frontend memanggil 129 path unik; 87 di antaranya TIDAK ADA di backend.

Router backend yang belum ada sama sekali: memory L1/L2/L3 workbench, knowledge RAG pipeline,
question-notebook, skills hub/tags, learning progress, plugins, book, voice, subagents detect,
auth profile/avatar/users, imports, solve.

## memory  (18 endpoint)
- /api/v1/memory/doc/L2/{X}
- /api/v1/memory/doc/L3/{X}
- /api/v1/memory/doc/{X}/{X}
- /api/v1/memory/doc/{X}/{X}/lines
- /api/v1/memory/doc/{X}/{X}/update
- /api/v1/memory/overview
- /api/v1/memory/resolve_entry/{X}
- /api/v1/memory/runs
- /api/v1/memory/runs/start
- /api/v1/memory/runs/{X}
- /api/v1/memory/runs/{X}/cancel
- /api/v1/memory/runs/{X}/events
- /api/v1/memory/runs/{X}/undo
- /api/v1/memory/settings
- /api/v1/memory/snapshot/{X}
- /api/v1/memory/snapshot/{X}/changes
- /api/v1/memory/snapshot/{X}/refresh
- /api/v1/memory/trace/kb

file FE: app/(utility)/memory/resolve/page.tsx, app/(utility)/settings/memory/page.tsx, components/memory/MemoryHub.tsx, components/memory/MemoryL1Workbench.tsx, components/memory/MemoryRunPanel.tsx, components/memory/MemorySection.tsx, components/memory/MemoryWorkbench.tsx, components/memory/useMemoryRun.ts, lib/memory-graph.ts

## knowledge  (16 endpoint)
- /api/v1/knowledge/connect-folder
- /api/v1/knowledge/connect-lightrag-server
- /api/v1/knowledge/connect-obsidian
- /api/v1/knowledge/create
- /api/v1/knowledge/default/{X}
- /api/v1/knowledge/probe-folder
- /api/v1/knowledge/probe-lightrag-server
- /api/v1/knowledge/rag-pipelines/active-model
- /api/v1/knowledge/rag-pipelines/llamaindex/config
- /api/v1/knowledge/rag-pipelines/model-options
- /api/v1/knowledge/rag-pipelines/pageindex/config
- /api/v1/knowledge/rag-pipelines/{X}/config
- /api/v1/knowledge/rag-pipelines/{X}/preflight
- /api/v1/knowledge/rag-providers
- /api/v1/knowledge/rag-providers/{X}
- /api/v1/knowledge/supported-file-types

file FE: lib/knowledge-api.ts

## question-notebook  (8 endpoint)
- /api/v1/question-notebook/categories
- /api/v1/question-notebook/categories/{X}
- /api/v1/question-notebook/entries/lookup/by-question
- /api/v1/question-notebook/entries/upsert
- /api/v1/question-notebook/entries/{X}
- /api/v1/question-notebook/entries/{X}/categories
- /api/v1/question-notebook/entries/{X}/categories/{X}
- /api/v1/question-notebook/entries{X}

file FE: lib/notebook-api.ts

## auth  (6 endpoint)
- /api/v1/auth/avatar
- /api/v1/auth/avatar/{X}
- /api/v1/auth/profile
- /api/v1/auth/profile/avatar
- /api/v1/auth/users
- /api/v1/auth/users/{X}

file FE: lib/admin-api.ts, lib/avatar.ts, lib/profile-api.ts

## notebook  (6 endpoint)
- /api/v1/notebook
- /api/v1/notebook/add_record_with_summary
- /api/v1/notebook/create
- /api/v1/notebook/list
- /api/v1/notebook/{X}
- /api/v1/notebook/{X}/records/{X}

file FE: components/notebook/SaveToNotebookModal.tsx, components/notebook/useNotebookSelection.ts, lib/notebook-api.ts

## skills  (6 endpoint)
- /api/v1/skills/hub/catalog{X}
- /api/v1/skills/hub/detail
- /api/v1/skills/install
- /api/v1/skills/tags/create
- /api/v1/skills/tags/list
- /api/v1/skills/tags/{X}

file FE: lib/skills-api.ts

## subagents  (5 endpoint)
- /api/v1/subagents/backends/options
- /api/v1/subagents/backends/{X}
- /api/v1/subagents/connections/{X}
- /api/v1/subagents/detect
- /api/v1/subagents/partners

file FE: lib/subagents-api.ts

## settings  (4 endpoint)
- /api/v1/settings/enabled-tools
- /api/v1/settings/mcp
- /api/v1/settings/providers/openai-codex
- /api/v1/settings/voice-autoplay

file FE: app/(utility)/settings/tools/page.tsx, hooks/useVoiceAutoplay.ts, lib/codex-oauth.ts, lib/mcp-api.ts

## learning  (3 endpoint)
- /api/v1/learning/progress
- /api/v1/learning/progress/{X}
- /api/v1/learning/progress/{X}/init-modules

file FE: lib/learning-api.ts

## chat  (3 endpoint)
- /api/v1/chat/sessions/{X}/branch-selection
- /api/v1/chat/sessions/{X}/messages/{X}
- /api/v1/chat/sessions/{X}/quiz-results

file FE: lib/session-api.ts

## plugins  (3 endpoint)
- /api/v1/plugins/capabilities/{X}/execute-stream
- /api/v1/plugins/list
- /api/v1/plugins/tools/{X}/execute-stream

file FE: app/(workspace)/playground/page.tsx

## book  (2 endpoint)
- /api/v1/book
- /api/v1/book/books

file FE: components/sidebar/BookRecent.tsx, lib/book-api.ts

## voice  (2 endpoint)
- /api/v1/voice/stt
- /api/v1/voice/tts

file FE: components/chat/home/ChatMessages.tsx, hooks/useVoiceRecorder.ts

## solve  (1 endpoint)
- /api/v1/solve

file FE: lib/api.ts

## imports  (1 endpoint)
- /api/v1/imports/chat-history

file FE: lib/chat-import/types.ts, lib/imports-api.ts

## co_writer  (1 endpoint)
- /api/v1/co_writer

file FE: lib/co-writer-api.ts

## question  (1 endpoint)
- /api/v1/question/judge

file FE: lib/quiz-judge.ts

## ws  (1 endpoint)
- /api/v1/ws

file FE: lib/unified-ws.ts
