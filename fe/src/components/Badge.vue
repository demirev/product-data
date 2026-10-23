<script setup>
// `tone` is a role, not a color: `unknown` has to be visually distinct from
// `no` rather than a paler version of it, because "we could not tell" is a real
// answer here and not a soft negative.
defineProps({ tone: { type: String, default: 'neutral' } })
</script>

<template>
	<span class="badge" :class="`badge--${tone}`"><slot /></span>
</template>

<style scoped>
.badge {
	display: inline-flex;
	align-items: center;
	gap: var(--space-1);
	padding: 2px 8px;
	border: 1px solid var(--border);
	border-radius: 999px;
	background: var(--surface-sunk);
	color: var(--ink-2);
	font-size: var(--text-xs);
	font-weight: 500;
	white-space: nowrap;
}

.badge--ai {
	background: var(--accent-soft);
	border-color: var(--accent);
	color: var(--accent);
	font-weight: 600;
}

.badge--yes {
	border-color: var(--border-strong);
	color: var(--ink);
	font-weight: 600;
}

/* Not a badge at all: no fill, a dashed edge. A reader scanning a grid should
   never mistake "unknown" for a decided "private". */
.badge--unknown {
	background: transparent;
	border-style: dashed;
	color: var(--ink-muted);
}
</style>
