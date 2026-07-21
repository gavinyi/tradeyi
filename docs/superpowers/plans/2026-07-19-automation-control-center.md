# Automation Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the strategy console the only page that can control services, global execution, and strategy scheduling.

**Architecture:** Existing service APIs and settings remain unchanged. Streamlit write controls move from monitoring and strategy pages into page 15; all other pages become read-only consumers with navigation links.

**Tech Stack:** Python 3.11, Streamlit, SQLite settings, pytest, Ruff.

## Global Constraints

- Futu remains `TrdEnv.SIMULATE` only.
- Every control write requires administrator authorization and an audit event.
- Monitoring and business pages must not retain duplicate write controls.
- Do not add `use_container_width` or custom CSS.

---

### Task 1: Control-location contract tests

**Files:**
- Create: `tests/test_automation_control_center.py`

**Interfaces:** Static source contract verifies the single write location.

- [ ] Write tests asserting page 15 owns service/radar controls and global mode writes.
- [ ] Write tests asserting pages 6, 5, and 10 no longer own duplicate controls.
- [ ] Run the focused test and confirm it fails before production edits.

### Task 2: Upgrade the strategy console

**Files:**
- Modify: `pages/15_策略控制台.py`
- Modify: `app.py`

**Interfaces:** Consumes `service_control`, `radar_control`, and existing TradingStore settings.

- [ ] Add service/radar status and start/stop/restart controls with audit events.
- [ ] Add global pause and execution-mode controls.
- [ ] Keep the existing strategy editor as the only per-strategy scheduling writer.
- [ ] Rename navigation and page copy to “自动化控制中心”.
- [ ] Run focused tests and confirm they pass.

### Task 3: Make monitoring and strategy pages read-only

**Files:**
- Modify: `pages/6_系统监控.py`
- Modify: `pages/5_财报Gamma策略.py`
- Modify: `pages/10_波动率期权策略.py`

**Interfaces:** These pages display status and link to `pages/15_策略控制台.py`.

- [ ] Remove duplicate write controls and now-unused imports/admin dependencies.
- [ ] Add status summaries and page links.
- [ ] Run focused tests and page static checks.

### Task 4: Verification

**Files:** No production changes.

- [ ] Run Ruff.
- [ ] Run all pytest tests.
- [ ] Run Streamlit page checker and verify the running app health endpoint.

