# Synthetic hook examples

These JSON arrays are **synthetic**, based on the event interfaces reviewed on 2026-09-05 in the [Codex reference](https://learn.chatgpt.com/docs/hooks) and [Claude reference](https://code.claude.com/docs/en/hooks). They are not captured sessions, authoritative vendor schemas, or evidence that a particular installed version emits every field.

The shell response examples are deliberately opaque JSON/string values, not assertions about a universal shell output schema. Optional fields may be absent; Codex turn/model fields and Claude permission fields are omitted to exercise incomplete inputs. Future normalization tests (WD-003/006) must preserve unknown values and provenance instead of rejecting these examples or fabricating IDs.

Coverage: session start, prompt, tool start/result/failure, compaction, subagent start/stop, stop; additionally Codex interrupt/session end and Claude notification. No real paths, credentials, prompts, or outputs are included. A fixture child ID does not imply a live subagent was launched.

Live evidence is recorded separately in [the compatibility report](../../../docs/provider-compatibility.md). Do not promote these examples to live fixtures or claim schema validation merely because they parse as JSON.
