// Project agent modes for the dedupe repo: GPT-5.6 Sol and Claude Opus 5.
// Modeled on the official grok-45-mode plugin (~/.config/amp/plugins/grok-45-mode.ts).
// @amp-agent-mode {"key":"gpt56sol","label":"GPT-5.6 Sol"}
// @amp-agent-mode {"key":"opus5","label":"Claude Opus 5"}

import type { PluginAPI } from '@ampcode/plugin'

const PROMPT = 'You are Amp. Help the user complete software engineering tasks.'

// Built-in tool names from `amp plugins show-agent-options`.
const DEEP_TOOL_NAMES = [
	'apply_patch',
	'create_file',
	'edit_file',
	'find_thread',
	'finder',
	'librarian',
	'oracle',
	'painter',
	'Read',
	'read_mcp_resource',
	'read_thread',
	'read_web_page',
	'shell_command',
	'shell_command_status',
	'skill',
	'Task',
	'view_media',
	'web_search',
] as const

export default function (amp: PluginAPI) {
	const gpt56Sol = amp.createAgent({
		name: 'gpt-5-6-sol',
		model: 'openai/gpt-5.6-sol',
		instructions: PROMPT,
		tools: DEEP_TOOL_NAMES,
		reasoningEffort: 'high',
		display: { label: 'GPT-5.6 Sol', color: '#2563eb' },
	})

	amp.registerAgentMode({
		key: 'gpt56sol',
		label: 'GPT-5.6 Sol',
		description: 'GPT-5.6 Sol with deep-mode tools and a minimal prompt',
		color: '#2563eb',
		agent: gpt56Sol.definition,
	})

	const opus5 = amp.createAgent({
		name: 'claude-opus-5',
		model: 'anthropic/claude-opus-5',
		instructions: PROMPT,
		tools: DEEP_TOOL_NAMES,
		reasoningEffort: 'high',
		display: { label: 'Claude Opus 5', color: '#d97706' },
	})

	amp.registerAgentMode({
		key: 'opus5',
		label: 'Claude Opus 5',
		description: 'Claude Opus 5 with deep-mode tools and a minimal prompt',
		color: '#d97706',
		agent: opus5.definition,
	})
}
