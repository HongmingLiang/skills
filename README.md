# skills

Agent skills, one directory each under `skills/`.

| Skill | What it does |
| --- | --- |
| [nylas-tasks](skills/nylas-tasks/) | Read-only Nylas mail and calendar access for agents |

Each skill carries its own `README.md`, `SKILL.md`, rules and scripts; install a
single one with `-s`, for example:

```bash
npx skills add HongmingLiang/skills -s nylas-tasks -y
```
