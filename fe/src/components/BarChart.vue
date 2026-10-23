<script setup>
import { computed } from 'vue'
import { Bar } from 'vue-chartjs'

import { axis, baseOptions, palette, scheme } from '../charts.js'
import { formatNumber } from '../api.js'

// One measure, one series, one color. Shading the bars by their own length
// would spend the color channel on what the bar length already says.
const props = defineProps({
	title: String,
	caption: String,
	// [{label, n}], any order — sorted here, largest first.
	items: { type: Array, default: () => [] },
	unit: { type: String, default: 'Releases' },
	loading: Boolean,
})

const rows = computed(() => [...props.items].sort((a, b) => b.n - a.n))
const colors = computed(() => (scheme.value, palette()))

const data = computed(() => ({
	labels: rows.value.map((row) => row.label),
	datasets: [{
		label: props.unit,
		data: rows.value.map((row) => row.n),
		backgroundColor: colors.value.series[0],
		borderRadius: { topLeft: 0, bottomLeft: 0, topRight: 4, bottomRight: 4 },
		borderSkipped: false,
		barThickness: 14,
	}],
}))

const options = computed(() => ({
	...baseOptions(colors.value),
	indexAxis: 'y',
	interaction: { mode: 'nearest', intersect: true },
	scales: {
		x: axis(colors.value, { title: props.unit, grid: true, ticks: { precision: 0 } }),
		y: axis(colors.value, { ticks: { crossAlign: 'far', autoSkip: false } }),
	},
}))

const height = computed(() => Math.max(160, rows.value.length * 30 + 60))
</script>

<template>
	<figure class="chart panel">
		<figcaption>
			<h2>{{ title }}</h2>
			<p v-if="caption" class="small muted">{{ caption }}</p>
		</figcaption>

		<div class="chart__canvas" :style="{ height: `${height}px` }">
			<div v-if="loading" class="skeleton chart__skeleton" />
			<p v-else-if="!rows.length" class="empty muted">Nothing to plot yet.</p>
			<Bar v-else :data="data" :options="options" :aria-label="`Bar chart: ${title}`" />
		</div>

		<details class="table">
			<summary class="small">Show the numbers</summary>
			<table>
				<thead>
					<tr><th scope="col">Category</th><th scope="col">{{ unit }}</th></tr>
				</thead>
				<tbody>
					<tr v-for="row in rows" :key="row.label">
						<th scope="row">{{ row.label }}</th>
						<td class="tabular">{{ formatNumber(row.n) }}</td>
					</tr>
				</tbody>
			</table>
		</details>
	</figure>
</template>

<style scoped>
.chart {
	margin: 0;
	padding: var(--space-4) var(--space-5) var(--space-5);
	min-width: 0;
}
.chart figcaption p { margin: var(--space-1) 0 0; }
.chart__canvas { margin-top: var(--space-4); position: relative; }
.chart__skeleton { height: 100%; }
.empty { display: grid; place-items: center; height: 100%; }

.table { margin-top: var(--space-3); }
.table summary { cursor: pointer; color: var(--ink-2); }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); margin-top: var(--space-3); }
th, td { text-align: left; padding: 5px var(--space-3); border-bottom: 1px solid var(--border); }
td, thead th + th { text-align: right; }
thead th { color: var(--ink-muted); font-weight: 600; }
</style>
