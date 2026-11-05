# agent_test

Agent engineering practice in Python. The three stages follow the weather and math assistant, research agent, and production projects from the [original roadmap](https://newmitbbs.com/t/1100784).

The default demos use fixtures. Follow the code in the order below, then run a failure case and explain what you would change for a real service.

## Setup

The labs were tested on macOS with Python 3.13.

```zsh
cd "$HOME/agent_test"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-labs.txt
python -m unittest discover -q
```

Use `requirements.txt` for the weather and math lab, the direct/ReAct/LangGraph research demos, and the local monitoring worker. The full install adds Chroma, AutoGen, and Google OAuth. The default test suite runs locally. It uses HTTP fixtures for authenticated providers.

## Stage 1 · Model, tools, and memory

```zsh
python -m stage_01_model_tools_py --trace \
  --prompt 'Weather in San Jose and calculate (12 + 8) / 4'
python -m stage_01_model_tools_py --chain
```

In the interactive session, enter `calculate 18 / 2`, then `What did you calculate?`. Try `/reset` and ask again. Also try `calculate 1/0` to see a tool error make its way back to the answer.

1. Read [agent_py.py](stage_01_model_tools_py/agent_py.py). Follow the user message, model reply, tool dispatch, and observation. Notice why a tool result carries the same call ID as the original request.
2. Read [tools_py.py](stage_01_model_tools_py/tools_py.py) and [transport_py.py](stage_01_model_tools_py/transport_py.py). Trace a malformed argument and an HTTP 429. The first returns a tool error and the second may be retried within a deadline.
3. Compare [chain_py.py](stage_01_model_tools_py/chain_py.py) with the explicit loop. Run the same prompt on both and inspect the tests for parallel tool calls and memory trimming.

For live weather, add `--live-weather` to the first command. For the live OpenAI model, set `OPENAI_API_KEY` and `OPENAI_MODEL` in your shell and add `--live`. Use a model your account can access that supports function calling and structured outputs. The agent bounds the number of turns and tool calls. The session retains a window of complete turns in process memory. [Official OpenAI function calling guide](https://developers.openai.com/api/docs/guides/function-calling)

## Stage 2 · ReAct, retrieval, and framework comparison

```zsh
python -m stage_02_agent_patterns_rag_py --engine react \
  'How does an outbox use a stable delivery key?'
python -m stage_02_agent_patterns_rag_py --compare --details
python -m stage_02_agent_patterns_rag_py --chroma .agent_lab/chroma --engine graph
```

1. Read [index_py.py](stage_02_agent_patterns_rag_py/index_py.py). Track a Markdown passage through chunking, embedding, top-k retrieval, and evidence ID assignment. The default hash embedding gives you a repeatable baseline for finding retrieval misses.
2. Read [engines_py.py](stage_02_agent_patterns_rag_py/engines_py.py). Compare the fixed pipeline, the bounded hand-written tool loop, a LangGraph state graph, and a three-role AutoGen conversation. AutoGen is included for the framework comparison. [AutoGen project status](https://github.com/microsoft/autogen)
3. Read [report_py.py](stage_02_agent_patterns_rag_py/report_py.py) and [eval_py.py](stage_02_agent_patterns_rag_py/eval_py.py). The validator checks that quotes exist in their cited passages. Read the accepted claims yourself to decide whether the passages support them. Compare retrieval hits, expected citations, abstentions, failure rate, tokens, and latency on the same examples.

The fixture suite has five questions. One intentionally asks for information outside the corpus. The default hash baseline misses the retries source on the rate-limit question. Use `--details` to inspect the misses and rerun after changing chunk size, overlap, or top-k. Timings include framework initialization. Treat this small suite as a regression drill.

Use `--live` for live drafting. Add `--chroma .agent_lab/chroma --live-embeddings` to use `text-embedding-3-small`. The latter sends corpus chunks and search queries to the embedding API. Use only documents you are allowed to send. With live generation, `--rates INPUT_USD OUTPUT_USD` accepts prices per million tokens and estimates generation cost. Embedding costs are separate. [OpenAI embeddings reference](https://developers.openai.com/api/reference/resources/embeddings/methods/create)

For the paper exercise, sketch three flows. ReAct alternates between choosing actions and consuming observations. Plan-and-Solve drafts a plan before executing it. Self-Ask answers follow-up questions before composing a final answer. Pick one research question and explain how each approach would behave if the first search came back empty. [ReAct](https://arxiv.org/abs/2210.03629), [Plan-and-Solve](https://aclanthology.org/2023.acl-long.147/), [Self-Ask](https://aclanthology.org/2023.findings-emnlp.378/)

## Stage 3 · Monitoring worker and personal assistant

### Monitoring and recovery

```zsh
python -m stage_03_production_projects_py --verbose monitor
python -m stage_03_production_projects_py inbox
```

The LangGraph worker fetches RSS or Atom, cleans and deduplicates entries, classifies sentiment, persists a report, and delivers it to a separate SQLite inbox. The inbox is a stand-in for a notification service. Use `--feed` for an operator-selected HTTPS source, and add `--live-model` to classify with OpenAI. A transient model failure or invalid label set falls back to the keyword baseline and marks the report accordingly. [NASA example RSS feed](https://cneos.jpl.nasa.gov/feed/)

```zsh
python -m stage_03_production_projects_py monitor --job nasa-cneos \
  --feed https://cneos.jpl.nasa.gov/feed/news.xml
```

Run the crash drill in a fresh directory. The first process exits with status 75 after the receiver commits the report. The second uses the same report key, sees the receipt already exists, and records the acknowledgment.

```zsh
state=$(mktemp -d)
slot=2030-01-02T03:00:00+00:00
python -m stage_03_production_projects_py --state "$state" --verbose \
  monitor --slot "$slot" --crash-after-delivery
python -m stage_03_production_projects_py --state "$state" --verbose monitor --slot "$slot"
python -m stage_03_production_projects_py --state "$state" inbox
```

Expect `inserted: false` in the second run's delivery event and one report in the inbox. Read [monitor_py.py](stage_03_production_projects_py/monitor_py.py) and [storage_py.py](stage_03_production_projects_py/storage_py.py) in that order. A future external notification service must honor the same stable delivery key for this replay guarantee.

The CLI is a one-shot scheduler entry point. Configure an external scheduler to invoke `monitor --minutes 60` at your chosen frequency, using the same job name and an absolute state path. Each tick replays pending reports first, then processes the current UTC slot. For reminders, schedule `reminders-tick`. The lab models a single-host service with SQLite and a local receiver.

### Personal assistant

```zsh
python -m stage_03_production_projects_py briefing --city Seattle
python -m stage_03_production_projects_py remind --id practice-1 \
  --at 2030-01-02T09:00:00-08:00 'Practice explaining the report outbox'
python -m stage_03_production_projects_py reminders-tick
```

The briefing combines upcoming events and weather, then suggests what to wear. The example reminder is delivered after its scheduled time. Set `--at` to a timestamp in the past if you want to see an immediate delivery when you tick the worker. Inspect it with `inbox`.

For live Calendar, create a Desktop OAuth client in a Google test project, enable the Calendar API, and grant access with a test account. Keep the downloaded client JSON outside the repository. [Google Calendar Python quickstart](https://developers.google.com/workspace/calendar/api/quickstart/python)

```zsh
python -m stage_03_production_projects_py authorize --credentials /path/to/credentials.json
python -m stage_03_production_projects_py briefing --google --live-weather --city Seattle
```

For Gmail headers, enable the Gmail API and add `--gmail` to both commands. The optional scope reads sender and subject metadata. The OAuth token is stored with mode 0600 in the local state directory and refreshed before requests when needed. Calendar and Gmail data stay in the local process. [Google Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)

## Practice explaining the tradeoffs

- **Grounding:** Show the difference between failing to retrieve a source and citing a passage that fails to support the answer.
- **Multiple agents:** Show the message that passes between the researcher and author. Explain where you would put the authoritative state if their views differed.
- **Latency:** Identify the slowest step using the trace. Explain which calls can overlap and what a timeout does to the user-visible outcome.
- **Reliability:** Reproduce the crash drill and explain why the receiver's idempotency key is essential after a worker restart.
