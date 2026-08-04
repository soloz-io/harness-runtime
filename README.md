# harness-runtime

Harness runtime expects multi-agents DAG in defined interface.

## Filesystem Docker Image
agents/
└── <agent-name>/
    ├── agent.ts            # Optional: model and runtime config
    ├── instructions     # Required: the always-on system prompt
        ├── 0*.<name>.md     # Required: Atleast 1. ALl under this folder will be combined together to single instruction.
    ├── tools/              # Optional: typed functions the model can call
    │   └── get_weather.ts
    ├── skills/             # Optional: procedures loaded on demand
    │   └── plan_a_trip.md
    ├── channels/           # Optional: message channels (HTTP, Slack, Discord)
    │   └── slack.ts
    └── schedules/          # Optional: recurring cron jobs
        └── weekly_recap.ts
server/
└── k8s/
└── scripts/
└── src/
    └── chat.ts/
└── Dockerfile
└── package.json