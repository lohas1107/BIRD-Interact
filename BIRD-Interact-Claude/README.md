# BIRD-Interact-Claude

Claude Agent SDK implementation of the BIRD-Interact text-to-SQL benchmark.
It supports both agentic (`a-interact`) and conversational (`c-interact`)
evaluation while authenticating through an existing Claude Code subscription.

## Architecture

```text
orchestrator/runner.py
  ├─ system_agent :6000   ClaudeSDKClient + task-scoped MCP tools
  ├─ user_simulator :6001 Claude Agent SDK, no tools
  └─ db_environment :6002 PostgreSQL execution and evaluation
```

The system agent exposes only the benchmark MCP tools. Claude Code's file,
shell, web, skill, and subagent tools are disabled so the agent cannot inspect
the dataset or ground-truth answers. Bird-coin accounting, phase transitions,
clarification limits, trajectories, and rewards live in task-scoped Python
state rather than model-controlled prompts.

## Requirements

- Python 3.10+
- Docker, for the BIRD-Interact PostgreSQL image
- Claude Code authenticated with a Pro, Max, Team, or Enterprise subscription

Install dependencies with Python only:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The `claude-agent-sdk` wheel includes a compatible Claude Code CLI. Log in once:

```bash
claude
# Run /login and select the subscription account, then use /status to verify it.
```

Do not set `ANTHROPIC_API_KEY`. Claude Code gives an API key precedence over a
subscription and would incur pay-as-you-go charges. Both SDK call paths reject
startup/calls when that variable is present.

## PostgreSQL setup

The evaluation services expect a BIRD-Interact PostgreSQL database to be
available before `scripts/start_services.sh` is run. The Compose file provides
the Lite database on host port `5432` and the Full database on host port `5433`.

For the Lite dataset (300 tasks, 18 databases):

```bash
docker compose up -d postgresql
docker compose logs -f postgresql
# Stop following logs after seeing: database system is ready to accept connections
```

For the Full dataset (600 tasks, 26 databases):

```bash
docker compose --profile full up -d postgresql_full
docker compose --profile full logs -f postgresql_full
# Stop following logs after seeing: database system is ready to accept connections
```

Set `DATASET=full` and `PG_PORT=5433` in `.env` when using the Full database.
When the application services run inside a Docker network, use the matching
container name as `PG_HOST` (`bird_interact_postgresql` or
`bird_interact_postgresql_full`). From a normal host, use `PG_HOST=127.0.0.1`.

## Configuration

```bash
cp .env.example .env
```

Defaults use the subscription aliases `sonnet` for the evaluated agent and
`haiku` for the user simulator. In a Docker-networked development container,
the database may need `PG_HOST=bird_interact_postgresql`; on a normal host with
published PostgreSQL port 5432, keep `PG_HOST=127.0.0.1`. For the Full database,
use the published port 5433 instead.

## Dataset

| Version | Tasks | Databases | PostgreSQL image | Dataset |
| --- | ---: | ---: | --- | --- |
| **Lite** | 300 | 18 | `shawnxxh/bird-interact-postgresql:latest` | [bird-interact-lite](https://huggingface.co/datasets/birdsql/bird-interact-lite) |
| **Full** | 600 | 26 | `shawnxxh/bird-interact-postgresql-full:latest` | [bird-interact-full](https://huggingface.co/datasets/birdsql/bird-interact-full) |

### Download and setup

Clone the dataset you want into the Claude implementation's repository root:

```bash
# Lite
git clone https://huggingface.co/datasets/birdsql/bird-interact-lite bird-interact-lite

# Full (use this instead of Lite when DATASET=full)
git clone https://huggingface.co/datasets/birdsql/bird-interact-full bird-interact-full
```

The public dataset normally does not include the `sol_sql` and `test_cases`
fields required for scored evaluation. Email
[bird.bench25@gmail.com](mailto:bird.bench25@gmail.com) with
`[bird-interact-lite GT&Test Cases]` or `[bird-interact-full GT&Test Cases]` in
the subject to obtain the corresponding ground-truth file.

Combine the public JSONL and ground truth into a separate output file first,
then replace the dataset's JSONL after checking the output. Do not use the same
path as both the input and output because the helper opens the output for
writing before it finishes reading the input.

```bash
python scripts/combine_public_with_gt.py \
  bird-interact-lite/bird_interact_data.jsonl \
  /path/to/bird_interact_gt_kg_testcases.jsonl \
  bird-interact-lite/bird_interact_data.with_gt.jsonl
mv bird-interact-lite/bird_interact_data.with_gt.jsonl \
   bird-interact-lite/bird_interact_data.jsonl
```

Replace `bird-interact-lite` with `bird-interact-full` for the Full dataset.
Each dataset directory contains `bird_interact_data.jsonl` and per-database
directories with schema, column meanings, and external knowledge.

## Run

```bash
# Start the three application services after PostgreSQL is ready.
bash scripts/start_services.sh

# Validate the evaluator without an LLM call
python -m orchestrator.runner --mode oracle --limit 1 --concurrency 1

# Subscription-backed smoke tests
python -m orchestrator.runner --mode a-interact --limit 1 --concurrency 1
python -m orchestrator.runner --mode c-interact --limit 1 --concurrency 1

# Evaluate all tasks in the dataset selected by DATASET in .env.
python -m orchestrator.runner --mode a-interact --concurrency 1
python -m orchestrator.runner --mode c-interact --concurrency 1

# Evaluate a selected number of tasks.
python -m orchestrator.runner --mode a-interact --limit 10 --concurrency 1
```

Start with concurrency 1 because parallel Claude SDK clients share the same
subscription limits. Result JSON is written under `results/` and includes
phase metrics, `tool_trajectory`, `agent_events`, elapsed time, and budget use.

### Tool profiles

Agent tools are selected once per evaluation from `config/tool_profiles.json`.
The file is a JSON object whose keys are unique profile names and whose values
are ordered, duplicate-free arrays of tool names. The built-in defaults are
`a-interact-default` for a-interact and `c-interact-default` for c-interact.
Available names are:

```text
execute_sql
get_schema
get_all_column_meanings
get_column_meaning
get_all_external_knowledge_names
get_knowledge_definition
get_all_knowledge_definitions
ask_user
submit_sql
```

Select a profile or a different JSON file with either the unified runner or an
a/c standalone runner:

```bash
python -m orchestrator.runner --mode a-interact --tool-profile my-profile
python -m orchestrator.runner --mode c-interact \
  --tool-profiles-file /path/to/tool_profiles.json --tool-profile semantic-c
python -m orchestrator.ainteract --tool-profile my-profile
python -m orchestrator.cinteract --tool-profile semantic-c
```

An empty tool array is valid, as are experimental profiles missing
`ask_user` or `submit_sql`. Unknown/duplicate tools or profiles, malformed
JSON, and invalid value types fail before evaluation tasks start. Tool order is
preserved in the MCP server and SDK allowlist. c-interact always preloads its
complete schema and external knowledge regardless of its selected tools.
Resolved profile metadata is stored once at the top level of a/c result JSON.

Oracle bypasses the agent and MCP server. It does not load this configuration,
and rejects both profile flags if either is explicitly supplied.

## View results

The runner writes JSON results as `results/eval_<mode>.json` by default. Generate
an interactive HTML report from any result file:

```bash
python -m orchestrator.report results/eval_a_interact.json
python -m orchestrator.report results/eval_c_interact.json
```

The report is written next to the JSON input with an `.html` extension. To
validate the service endpoints and database lifecycle without an LLM call, run:

```bash
python -m orchestrator.test_harness --limit 1 --concurrency 1
```

## Evaluation modes

### a-interact (Agentic Interaction)

The agent autonomously chooses among the benchmark tools, including schema and
knowledge lookup, read-only SQL execution, clarification with `ask_user`, and
final submission with `submit_sql`. The per-task bird-coin budget is:

```text
6 + 2 * number_of_ambiguities + 2 * patience
```

The default `patience` is 3. After a successful Phase 1 submission, the same
persistent agent session continues with the follow-up returned by the tool and
can submit Phase 2.

### c-interact (Conversational Interaction)

The orchestrator drives the phase structure and, by default, exposes only
`ask_user` and `submit_sql` to the agent. The clarification limit is:

```text
number_of_critical_ambiguities + number_of_knowledge_ambiguities + patience
```

The workflow is:

1. **Phase 1**: Ask clarifying questions up to the limit, submit SQL, and allow
   one debug retry if the submission is incorrect or not executable.
2. **Phase 2**: If Phase 1 succeeds and the task has a follow-up, transition to
   the follow-up question, submit SQL, and allow one debug retry.

## Results

The Claude runner records per-task phase results, rewards, elapsed time, budget
usage, tool trajectories, and agent events in the JSON output. The following
table is retained from the ADK implementation as a reference comparison; it is
not a benchmark result for this Claude implementation.

Evaluated on BIRD-Interact-Lite (300 tasks), Claude Sonnet 4.5, patience=3,
v1 user simulator prompt (claude-haiku-4-5):

| Mode | P1 (%) | P2 (%) | Avg Reward |
| --- | ---: | ---: | ---: |
| **c-interact (ADK)** | 44.67 | 30.67 | 0.395 |
| **c-interact (reference)** | 40.47 | 27.09 | — |
| **a-interact (ADK)** | 36.67 | 23.67 | 0.328 |
| **a-interact (reference)** | 37.67 | 22.00 | — |

## Project layout

```text
system_agent/
  agent.py           prompts
  claude_runtime.py  persistent task sessions and SDK message capture
  tools.py           task-scoped in-process MCP tools
  server.py          FastAPI session API
user_simulator/      two-stage simulated user, backed by no-tools SDK calls
db_environment/      isolated PostgreSQL execution and evaluation
orchestrator/        a-interact, c-interact, oracle, batch runner and reports
shared/              settings, database utilities and simulator LLM wrapper
```
