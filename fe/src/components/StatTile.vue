<script setup>
defineProps({
	label: String,
	value: [String, Number],
	hint: String,
	loading: Boolean,
})
</script>

<template>
	<div class="tile panel">
		<div class="tile__label">{{ label }}</div>
		<div v-if="loading" class="skeleton tile__skeleton" />
		<div v-else class="tile__value">{{ value }}</div>
		<div v-if="hint && !loading" class="tile__hint small muted">{{ hint }}</div>
	</div>
</template>

<style scoped>
.tile {
	padding: var(--space-4) var(--space-5);
	min-width: 0;
}
.tile__label {
	font-size: var(--text-xs);
	font-weight: 600;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--ink-muted);
}
.tile__value {
	margin-top: var(--space-2);
	font-size: var(--text-2xl);
	font-weight: 600;
	line-height: 1.1;
	overflow-wrap: anywhere;
}
.tile__hint { margin-top: var(--space-1); }
.tile__skeleton { margin-top: var(--space-2); height: 38px; width: 70%; }
</style>
