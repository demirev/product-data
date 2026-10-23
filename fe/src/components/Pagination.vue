<script setup>
import { computed } from 'vue'

import { formatNumber } from '../api.js'

const props = defineProps({
	page: { type: Number, default: 1 },
	perPage: { type: Number, default: 24 },
	total: { type: Number, default: 0 },
})
const emit = defineEmits(['go'])

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.perPage)))
const first = computed(() => (props.page - 1) * props.perPage + 1)
const last = computed(() => Math.min(props.page * props.perPage, props.total))
</script>

<template>
	<nav v-if="total > perPage" class="pager" aria-label="Pagination">
		<p class="small muted tabular">
			Showing {{ formatNumber(first) }}–{{ formatNumber(last) }} of {{ formatNumber(total) }}
		</p>
		<div class="pager__buttons">
			<button class="button" type="button" :disabled="page <= 1" @click="emit('go', page - 1)">
				Previous
			</button>
			<span class="small tabular">Page {{ formatNumber(page) }} of {{ formatNumber(pages) }}</span>
			<button class="button" type="button" :disabled="page >= pages" @click="emit('go', page + 1)">
				Next
			</button>
		</div>
	</nav>
</template>

<style scoped>
.pager {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-3);
	align-items: center;
	justify-content: space-between;
	margin-top: var(--space-5);
	padding-top: var(--space-4);
	border-top: 1px solid var(--border);
}
.pager p { margin: 0; }
.pager__buttons {
	display: flex;
	gap: var(--space-3);
	align-items: center;
}
</style>
