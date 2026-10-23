<script setup>
import { formatNumber } from '../api.js'

// Chips are the only place the active filter set is stated in words. A reader
// arriving from someone else's bookmarked URL should be able to see, without
// opening the panel, exactly which slice they are looking at.
defineProps({
	chips: { type: Array, default: () => [] },
	total: { type: Number, default: null },
	noun: { type: String, default: 'result' },
	plural: { type: String, default: 'results' },
	loading: Boolean,
})
const emit = defineEmits(['remove', 'clear'])
</script>

<template>
	<div class="chips">
		<p class="chips__count tabular">
			<template v-if="loading && total === null">Searching…</template>
			<template v-else>
				<strong>{{ formatNumber(total ?? 0) }}</strong>
				{{ total === 1 ? noun : plural }}
			</template>
		</p>

		<ul v-if="chips.length" class="chips__list">
			<li v-for="chip in chips" :key="chip.id">
				<button class="chip" type="button" @click="emit('remove', chip)">
					<span class="chip__key">{{ chip.label }}</span>
					<span class="chip__value">{{ chip.value }}</span>
					<span aria-hidden="true">×</span>
					<span class="sr-only">Remove filter</span>
				</button>
			</li>
		</ul>

		<button v-if="chips.length" class="button chips__clear" type="button" @click="emit('clear')">
			Clear all
		</button>
	</div>
</template>

<style scoped>
.chips {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-3);
	align-items: center;
	margin-bottom: var(--space-4);
}
.chips__count { margin: 0; font-size: var(--text-sm); color: var(--ink-2); }
.chips__count strong { font-size: var(--text-md); color: var(--ink); }

.chips__list {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-2);
	margin: 0;
	padding: 0;
	list-style: none;
}
.chip {
	display: inline-flex;
	align-items: baseline;
	gap: var(--space-2);
	padding: 3px 9px;
	border: 1px solid var(--border-strong);
	border-radius: 999px;
	background: var(--surface);
	font-size: var(--text-sm);
	cursor: pointer;
}
.chip:hover { background: var(--surface-sunk); }
.chip__key {
	font-size: var(--text-xs);
	text-transform: uppercase;
	letter-spacing: 0.05em;
	color: var(--ink-muted);
}
.chip__value { max-width: 30ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chips__clear { margin-left: auto; }
</style>
