name: token-efficiency
description: Optimize prompts, context, and outputs for minimal tokens without quality loss. Trigger on cost, verbosity, or context constraints.

# Principle
Maximize information density. Never trade off correctness or usability.

# Input Optimization
- Compress instructions: imperative, no filler, no redundancy
- Trim history: keep task, constraints, open decisions; drop resolved turns
- Summarize checkpoints: [CHECKPOINT] Task | Decision | Status
- Reference > repeat: label prior content instead of reinserting
- Limit examples: 0–2, input→output only
- Retrieve narrowly: inject only relevant slices, not full docs

# Output Optimization
- Match length to task (short answers stay short)
- No preamble, restatement, or postamble
- Use dense formats: bullets > prose, code > explanation
- Don’t echo input
- Code: minimal, no boilerplate or obvious comments

# Agent Workflows
- Summarize instead of accumulating context
- Compress tool outputs before reuse
- Avoid duplicate tool calls (cache results)

# Metrics
- Tokens/turn ↓
- Context utilization ↓
- Quality unchanged (validate on eval set)

# Red Flags
- Re-injecting full docs
- Verbose system prompts
- >2 examples
- Raw tool dumps
- Full history every turn