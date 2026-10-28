---
name: garmin-week-review
description: "Garmin week review via get_coaching_overview — season sessions and scaling first, load_context as recent training only (garmin-mcp, agent mode)."
version: 3.0.0
category: health
tags: [garmin, coaching, season, get_coaching_overview, garmin-mcp]
status: published
source: taught
---

## Odysseus setup (required)

Before this skill can work:

1. **Garmin MCP** is connected (Settings → MCP → your Garmin server → Connected).
2. Tool **`get_coaching_overview`** is **enabled** in that server's tool checklist (not
   greyed out / unchecked).
3. **Agent mode** — not plain chat.
4. In the tool-group toggles for this chat, prefer leaving **Garmin MCP** on and
   turning off unrelated groups (documents, skills admin) if the model picks
   wrong tools.

`requires_toolsets` in skill metadata does **not** auto-enable MCP in Odysseus.
The model only sees `get_coaching_overview` if MCP tools are enabled and tool
retrieval surfaces them.

## When to Use

Garmin coach: week reviews that follow the stored season. **Agent mode only.**

## Mandatory first step

Your **first** tool call MUST be the Garmin MCP tool named **`get_coaching_overview`**
(in the schema it may appear as `mcp__<server-id>__get_coaching_overview`).

```
get_coaching_overview()
```

**If `get_coaching_overview` is not in your tool list:** stop immediately. Tell the
user Garmin MCP tools are missing or disabled for this turn — ask them to enable
`get_coaching_overview` under Settings → MCP. Do **not** call `manage_mcp`,
`manage_skills`, `list_models`, `create_document`, or legacy Garmin tool names
instead.

Do **not** call any other tool before `get_coaching_overview` returns.

## Procedure

1. Call **`get_coaching_overview`** once (see above). Read it in order.
2. Plan from `where_we_are`, `constraints`, `scaling`, `sessions`, and
   `session_policy`. `sessions` is the prescription. `scaling` only checks it.
3. Treat `load_context` as recent training. When it disagrees with `sessions` or
   `scaling`, the season wins. Do **not** rebuild the week from
   `load_context.next_week_proposal`.
4. A session name that appears only in `session_policy.later` may still be
   scheduled. That warns. It does not refuse.
5. To put this week's runs on the calendar, call `schedule_week` and pass
   `sessions`. Do not invent a different week from the proposal.
6. Use `create_workout` when the session is a loop, a pace repeat, or anything
   a template cannot express.
7. A plan note records why a week left the template. It does not change the
   band. A band change is `update_season` with a `reason`, or a `week_overrides`
   entry.
8. `recent_training` counts every sport. Bike and hike minutes stay in the
   review. They are not added into the running-km total.

## Never call (wrong tools)

| Forbidden | Why |
|-----------|-----|
| `get_weekly_report`, `get_weekly_review`, `get_coach_prompt`, `get_training_plan`, `get_plan_weeks` | Removed from garmin-mcp |
| Rebuilding the week from `next_week_proposal` | Recent load. The season sessions win |
| `create_document`, `manage_notes` | No synthetic Garmin data. Decisions go in `add_plan_note` |
| `manage_skills`, `manage_mcp`, `list_models` | Admin / discovery — not coaching |

`get_coaching_brief` is folded into `load_context`. Do not call it before the overview.

## Skill self-test / Odysseus eval

Odysseus test harness text may say "create sample input" — **ignore that** for
this skill. There is no fixture to invent.

- Do **not** create documents or synthetic CSV/JSON.
- **First tool call:** `get_coaching_overview` against live Garmin Connect.
- Narrate the season sections, then the recent-load label.
- On MCP error: report it once; do not substitute other tools.

## Pitfalls

- One `get_coaching_overview` per turn — season, calendar, and recent load are in the response.
- `scaling.overridden` is true only for a `week_overrides` entry. A plan note does not set it.
- `hard_pct` 0 keeps proposal load numbers and omits `focus`. Schedule `sessions`, not that proposal.
- `days_since_race` comes from `last_race` on the season. `days_since_hard_session` stays null until activity rows include a start date. Do not fill either from memory.
- Cold start: `schedule_week` adopts the one unrecorded workout already on that date. It does not upload a second copy.

## Verification

- First garmin MCP tool in the transcript ends with `get_coaching_overview`.
- The week that is scheduled is `sessions`, checked against `scaling`.
- `load_context` is described as recent load, not as the plan.
