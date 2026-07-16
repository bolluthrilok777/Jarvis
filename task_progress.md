# Jarvis Core Actions v1 Fix - Task Progress

## Root Causes Identified

### A. rule_matcher.py (5 issues)
1. No rule for bare math expressions like "79 plus 8" (no prefix)
2. No rule for "do <math>" prefix
3. No rule for "open calculator and do <math>" (only calculate/compute/evaluate)
4. "note pad" (two words) not recognized as "notepad"
5. "calculated" suffix not handled

### B. normalizer.py (2 issues)
1. "hey" not in FILLER_PHRASES (only "hey jarvis" is)
2. "calculated" not stripped as filler

### C. task_models.py (2 issues)
1. `_summarize_output()` doesn't handle `"value"` key (calculator returns number → `{"value": 51.0}`)
2. `TaskPublic` has no `result` field, so UI `t.result` is always undefined

### D. orchestrator.py (1 issue)
1. No verification that calculator result contains numeric value before marking completed

### E. intent_classifier.py (1 issue)
1. "what is" prefix classified as conversational, but "what is 5 plus 3" is a math command

## Fix Checklist
- [x] Analyze all source files and identify root causes
- [ ] Fix normalizer.py: Add "hey", "calculated", "do" to filler phrases
- [ ] Fix rule_matcher.py: Add rules for bare math, "do" prefix, "note pad" alias, "calculated" suffix
- [ ] Fix task_models.py: Handle "value" key in _summarize_output, add result field to TaskPublic
- [ ] Fix orchestrator.py: Verify calculator result has numeric value before marking completed
- [ ] Fix intent_classifier.py: Don't classify math expressions as conversational
- [ ] Fix ui.html: Display step_results properly for calculator tasks
- [ ] Run existing tests to establish baseline
- [ ] Run full test suite to verify all fixes
- [ ] Manual verification of all 12 test scenarios